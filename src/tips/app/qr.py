"""Выпуск QR на чек (FR-1): проверки, nonce, срок, подпись, платёжная ссылка."""
import secrets
from dataclasses import dataclass

from tips.config import PAY_URL
from tips.domain.errors import PaymentError
from tips.domain.money import MAX_BASE, fmt
from tips.domain.qr_sign import QR_TTL, TIME_FMT, check_exp, compute_signature, verify_signature
from tips.domain.ids import new_id
from tips.domain.qr_v2 import QR_V2_TTL, new_nonce, qr_version, sign_v2, verify_v2
from tips.domain.validation import validate_qr_request

TIP_OPTIONS = [0, 5, 10, 15, 20]  # кнопки формы; «своя сумма» вводится отдельно, лимит 50 %
MAX_TIP_PCT = 50


@dataclass(frozen=True)
class V2Issue:
    """Параметры выпуска QR версии 2: связка ключей и адрес формы (ссылка с фрагментом: он не уходит на сервер)."""
    keys: object
    front_url: str


def create_qr(req, secret, store, now, renderer=None, amount_required=True, v2=None):
    """Выпуск QR на чек. Проверки: мерчант ACTIVE, официант этого мерчанта, чек не меняет сумму.

    amount_required=False (HTTP): суммы в запросе нет, чек должен уже лежать в реестре, иначе NOT_FOUND.
    """
    amount = validate_qr_request(req, amount_required)
    merchant_id, employee_id, receipt_id = req["merchant_id"], req["employee_id"], req["receipt_id"]
    status = store.merchant_status(merchant_id)
    if status is None:
        raise PaymentError("NOT_FOUND")
    if status != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    employee = store.employee(employee_id)
    if employee is None:
        raise PaymentError("NOT_FOUND")
    if employee["merchant_id"] != merchant_id:
        raise PaymentError("ACCESS_DENIED")
    if amount is None:
        amount = store.receipt_amount(merchant_id, receipt_id)
        if amount is None:
            raise PaymentError("NOT_FOUND")
    if not (0 < amount <= MAX_BASE):
        raise PaymentError("AMOUNT_OUT_OF_RANGE")

    # Чек под блокировкой: два одновременных запроса на один чек (двойной клик) не выпустят два живых QR,
    # потому что «один успешный платёж на QR» не защитит от оплаты каждого из двух QR по отдельности.
    with store.atomic():
        if store.receipt_amount(merchant_id, receipt_id) is None:  # касса передала новый чек
            store.add_receipt(merchant_id, receipt_id, fmt(amount))
        store.lock_receipt(merchant_id, receipt_id)
        known = store.receipt_amount(merchant_id, receipt_id)  # перечитано под блокировкой
        if known != amount:
            raise PaymentError("AMOUNT_MISMATCH")
        version = 2 if v2 else 1
        live = store.live_qr_for_receipt(merchant_id, receipt_id, now, version)
        if live is not None:  # на чек уже есть действующий QR — отдаём его, а не выпускаем второй
            if live["employee_id"] != employee_id:
                raise PaymentError("ACCESS_DENIED")
            return qr_result(live, new=False, renderer=renderer, v2=v2)
        if v2:
            qr = {"qr_id": new_id("qr"), "merchant_id": merchant_id, "employee_id": employee_id, "receipt_id": receipt_id,
                  "nonce": new_nonce(), "exp": (now + QR_V2_TTL).strftime(TIME_FMT), "version": 2,
                  "key_id": v2.keys.active_kid}
            qr["signature"] = sign_v2(v2.keys, qr, fmt(amount), "RUB")
        else:
            qr = {"qr_id": store.next_qr_id(), "merchant_id": merchant_id, "employee_id": employee_id,
                  "receipt_id": receipt_id, "nonce": secrets.token_hex(3), "exp": (now + QR_TTL).strftime(TIME_FMT)}
            qr["signature"] = compute_signature(secret, qr)
        store.add_qr(qr)
        return qr_result(qr, new=True, renderer=renderer, v2=v2)


def qr_result(qr, new, renderer=None, v2=None):
    """Платёжная ссылка с подписью (ADR-003) и, если задан и доступен рендерер, PNG с QR-кодом."""
    if v2:  # фрагмент (#...) браузер не отправляет ни на сервер, ни в Referer, ни в журналы доступа
        url = f"{v2.front_url}#q={qr['qr_id']}&s={qr['signature']}"
    else:
        url = f"{PAY_URL}?qr={qr['qr_id']}&sig={qr['signature']}"
    png = renderer.render(url, qr["qr_id"]) if renderer else None
    return {"qr_id": qr["qr_id"], "pay_url": url, "exp": qr["exp"], "new": new, "png": png}


def get_qr_view(qr_id, sig, secret, store, now):
    """Данные для формы чаевых (экраны 1-2). Порядок Q-1…Q-4 как у платежа; сумма чека из реестра, не от клиента."""
    qr = store.find_qr(qr_id)
    if qr is None or qr_version(qr) != 1 or not verify_signature(secret, qr, sig):  # неверная подпись не раскрывает qr_id
        raise PaymentError("INVALID_QR")
    if not check_exp(qr, now):
        raise PaymentError("QR_EXPIRED")
    if any(p["status"] == "SUCCESS" for p in store.payments_for_qr(qr_id)):
        raise PaymentError("QR_USED")
    merchant = store.merchant(qr["merchant_id"])
    if merchant is None or merchant["status"] != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    employee = store.employee(qr["employee_id"])
    amount = store.receipt_amount(qr["merchant_id"], qr["receipt_id"])
    if employee is None or amount is None:
        raise PaymentError("NOT_FOUND")
    return {"qr_id": qr_id, "merchant_id": merchant["merchant_id"], "merchant_name": merchant["name"],
            "employee_id": employee["employee_id"], "employee_name": employee["name"],
            "receipt_id": qr["receipt_id"], "base_amount": amount, "currency": "RUB",
            "tip_options": TIP_OPTIONS, "max_tip_pct": MAX_TIP_PCT, "exp": qr["exp"]}


def get_qr_view_v2(qr_id, sig, keys, store, now):
    """Форма чаевых для QR версии 2: без внутренних идентификаторов мерчанта и официанта (только названия)."""
    qr = store.find_qr(qr_id)
    amount = store.receipt_amount(qr["merchant_id"], qr["receipt_id"]) if qr else None
    if (qr is None or qr_version(qr) != 2 or amount is None
            or not verify_v2(keys, qr, fmt(amount), "RUB", sig)):  # неверная подпись не раскрывает, есть ли такой QR
        raise PaymentError("INVALID_QR")
    if not check_exp(qr, now):
        raise PaymentError("QR_EXPIRED")
    if any(p["status"] == "SUCCESS" for p in store.payments_for_qr(qr_id)):
        raise PaymentError("QR_USED")
    merchant = store.merchant(qr["merchant_id"])
    if merchant is None or merchant["status"] != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    employee = store.employee(qr["employee_id"])
    if employee is None:
        raise PaymentError("NOT_FOUND")
    return {"merchant_name": merchant["name"], "employee_name": employee["name"], "receipt_id": qr["receipt_id"],
            "base_amount": fmt(amount), "currency": "RUB", "tip_options": TIP_OPTIONS, "max_tip_percent": MAX_TIP_PCT,
            "exp": qr["exp"]}
