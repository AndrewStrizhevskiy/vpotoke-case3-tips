"""Платёж через провайдера (API v2): подтверждение, применение статуса, поздний успех, возвраты, входящие события.

Правила (дизайн: docs/product/v2-design.md, раздел 5):
  * вызовы провайдера идут вне транзакций и блокировок: сеть не должна держать строки базы;
  * ключ идемпотентности у провайдера и reference это payment_id: потеря ответа лечится запросом статуса по reference;
  * истину о платеже даёт провайдер (get_status), а не тело вебхука: событие лишь повод спросить;
  * изменения статуса идут под блокировкой QR (lock_qr), как при создании платежа: оплаты и отмены одного QR сериализуются;
  * вторая успешная оплата того же QR (в том числе поздний успех) не становится вторым SUCCESS: платёж уходит в
    REFUND_PENDING и в той же транзакции появляется запись возврата (outbox), которую исполняет воркер.
"""
import logging

from tips.adapters.provider import base as pb
from tips.domain.errors import PaymentError, StateChanged
from tips.domain.ids import new_id

log = logging.getLogger("tips.provider")
MAX_REFUND_ATTEMPTS = 5


def _qr_paid(store, payment):
    return any(p["status"] == "SUCCESS" and p["payment_id"] != payment["payment_id"]
               for p in store.payments_for_qr(payment["qr_id"]))


def confirm_payment_v2(payment, store, provider, payment_method_token, return_url, now):
    """CREATED -> PENDING -> ответ провайдера. Повторный confirm не вызывает провайдера."""
    if payment["status"] == "CANCELED":
        raise PaymentError("PAYMENT_STATE_CONFLICT")
    if payment["status"] != "CREATED":
        return payment
    outcome = None  # решение принимается под блокировкой, а действие (ответ, повтор) уже вне неё
    with store.atomic():
        store.lock_qr(payment["qr_id"])
        if _qr_paid(store, payment):  # этот QR уже оплачен другим платежом: подтверждать нечего, платёж закрываем
            try:
                store.transition(payment, "CANCELED", None, "payment_canceled", "system", now)
                outcome = "paid"
            except StateChanged:
                outcome = "retry"
        else:
            try:
                store.transition(payment, "PENDING", None, "payment_submitted", "client", now)
                # провайдер записывается до вызова: если ответ потеряется, сверка всё равно знает, у кого спрашивать
                store.set_provider_fields(payment["payment_id"], provider.name, None, None)
            except StateChanged:  # параллельный confirm или cancel успел раньше: отвечаем как на повтор
                outcome = "retry"
    if outcome == "paid":
        raise PaymentError("QR_USED")
    if outcome == "retry":
        return confirm_payment_v2(store.get_payment(payment["payment_id"]), store, provider, payment_method_token,
                                  return_url, now)
    try:
        result = provider.create_payment(payment["payment_id"], payment["amount_total"], "RUB", payment_method_token,
                                         return_url, payment["payment_id"])
    except pb.ProviderUnavailable:  # исход неизвестен: PENDING до сверки или вебхука
        log.warning("провайдер не ответил по платежу %s", payment["payment_id"])
        return store.get_payment(payment["payment_id"])
    return apply_provider_status(store, payment["payment_id"], result, provider, now, "client")


def _allowed_action(provider, url):
    from urllib.parse import urlsplit
    parts = urlsplit(url or "")
    secure = parts.scheme == "https" or (parts.scheme == "http" and getattr(provider, "allow_http_actions", False))
    return secure and parts.hostname in provider.action_hosts


def apply_provider_status(store, payment_id, result, provider, now, actor):
    """Привести платёж в соответствие с ответом провайдера. Идемпотентно: повтор того же ответа ничего не меняет."""
    for _ in range(3):  # конфликт переходов: перечитываем платёж и применяем ответ к актуальному состоянию
        try:
            with store.atomic():
                p = store.get_payment(payment_id)
                if p is None:
                    raise PaymentError("NOT_FOUND")
                store.lock_qr(p["qr_id"])
                p = store.get_payment(payment_id)
                if result.provider_payment_id:
                    store.set_provider_fields(payment_id, provider.name, result.provider_payment_id,
                                              p.get("next_action"), result.status)
                return _apply_locked(store, p, result, provider, now, actor)
        except StateChanged:
            continue
    return store.get_payment(payment_id)


def _apply_locked(store, p, result, provider, now, actor):
    status = p["status"]
    if result.status == pb.SUCCEEDED:
        if status in ("SUCCESS", "REFUND_PENDING", "REFUNDED"):
            return p
        if _qr_paid(store, p):  # деньги списаны второй раз: возврат вместо второго успеха
            store.transition(p, "REFUND_PENDING", None, "payment_refund_requested", "system", now)
            store.refund_create(new_id("rf"), p, now)
        elif status == "PENDING":
            store.set_provider_fields(p["payment_id"], None, None, None)
            store.transition(p, "SUCCESS", None, "payment_confirmed", actor, now)
        elif status in ("DECLINED", "CANCELED", "CREATED"):  # поздний успех: мы уже закрыли платёж, а банк списал
            store.transition(p, "SUCCESS", None, "payment_late_success", "system", now)
        return store.get_payment(p["payment_id"])
    if status != "PENDING":
        return p  # отказ, действие или «в обработке» для уже закрытого платежа ничего не меняют
    if result.status == pb.FAILED:
        store.set_provider_fields(p["payment_id"], None, None, None)
        store.transition(p, "DECLINED", result.error_code or "CARD_DECLINED", "payment_declined", actor, now)
    elif result.status == pb.ACTION_REQUIRED:
        if not _allowed_action(provider, result.action_url):  # чужой узел вместо банка: не отдаём клиенту такую ссылку
            log.error("провайдер вернул недопустимый адрес 3-D Secure по платежу %s", p["payment_id"])
            return p
        store.set_provider_fields(p["payment_id"], provider.name, result.provider_payment_id,
                                  {"type": "redirect", "url": result.action_url}, result.status)
    return store.get_payment(p["payment_id"])


def refresh_payment(store, payment, provider, now):
    """Опрос статуса сам обновляет PENDING у провайдера: 3-D Secure закрывается и без вебхука."""
    if payment["status"] != "PENDING":
        return payment
    result = provider.get_status(reference=payment["payment_id"],
                                 provider_payment_id=payment.get("provider_payment_id") or None)
    if result.status == pb.NOT_FOUND:
        return payment
    return apply_provider_status(store, payment["payment_id"], result, provider, now, "system")


def accept_webhook(store, provider, headers, raw_body, now):
    """Подпись -> входящий ящик -> обработка. Дубль принимается без повторной обработки. Возвращает True, если событие новое."""
    event = provider.parse_webhook(headers, raw_body, now)  # WebhookInvalid при неверной подписи
    is_new = store.inbox_add(provider.name, event, now)
    if is_new:
        process_event(store, provider, event.event_id, event.event_type, event.provider_payment_id, now)
    return is_new


def process_event(store, provider, event_id, event_type, provider_payment_id, now):
    """Обработка одного события; сбой не теряет его: воркер повторит. Событие лишь повод спросить провайдера."""
    try:
        if provider_payment_id:
            payment = store.payment_by_provider_ref(provider.name, provider_payment_id)
            if payment is None:  # событие пришло раньше, чем мы записали provider_payment_id: найдём по reference
                known = provider.get_status(provider_payment_id=provider_payment_id)
                payment = store.get_payment(known.reference) if known.reference else None
            if payment is not None and event_type.startswith("refund."):
                _finish_refund(store, payment, now)
            elif payment is not None:
                result = provider.get_status(reference=payment["payment_id"], provider_payment_id=provider_payment_id)
                apply_provider_status(store, payment["payment_id"], result, provider, now, "system")
        store.inbox_mark(provider.name, event_id, now)
        return True
    except Exception as exc:  # noqa: BLE001 - событие остаётся необработанным, воркер повторит позже
        log.exception("событие %s не обработано", event_id)
        store.inbox_mark(provider.name, event_id, now, error=type(exc).__name__)
        return False


def _finish_refund(store, payment, now):
    with store.atomic():
        store.lock_qr(payment["qr_id"])
        p = store.get_payment(payment["payment_id"])
        if p["status"] == "REFUND_PENDING":
            store.transition(p, "REFUNDED", None, "payment_refunded", "system", now)


def process_inbox(store, provider, now, limit=100):
    """Страховка вебхука: необработанные события из ящика. Возвращает число обработанных."""
    done = 0
    for row in store.inbox_pending(limit):
        if row["provider"] == provider.name and process_event(store, provider, row["event_id"], row["event_type"],
                                                              row["provider_payment_id"], now):
            done += 1
    return done


def process_refunds(store, provider, now, details=None):
    """Исполняет возвраты из очереди. Вызов провайдера вне транзакции; после MAX_REFUND_ATTEMPTS неудач нужен человек."""
    finished = []
    for r in store.refunds_open(max_attempts=MAX_REFUND_ATTEMPTS):
        if r["provider"] != provider.name or not r["provider_payment_id"]:
            continue
        try:
            result = provider.refund(r["provider_payment_id"], r["amount"], r["refund_id"])
        except pb.ProviderUnavailable:
            store.refund_mark(r["refund_id"], "PENDING", now, attempt=True)
            continue
        if result.status == "SUCCEEDED":
            store.refund_mark(r["refund_id"], "SUCCEEDED", now, result.provider_refund_id, attempt=True)
            _finish_refund(store, store.get_payment(r["payment_id"]), now)
            finished.append(r["refund_id"])
        else:
            store.refund_mark(r["refund_id"], "FAILED" if result.status == "FAILED" else "PENDING", now, attempt=True)
    stuck = [r["payment_id"] for r in store.refunds_failed(MAX_REFUND_ATTEMPTS)]
    if stuck and details is not None:
        details["refund_stuck"] = stuck
    return finished
