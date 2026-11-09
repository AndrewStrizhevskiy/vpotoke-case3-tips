"""Фоновые задачи над платежами: сверка зависших PENDING и автоотмена забытых CREATED (ADR-001/002).

Главное правило сверки: платёж закрывается отказом только по ответу шлюза, а не по одному истечению времени.
Если банк на самом деле списал деньги, а мы закрыли платёж как DECLINED, клиент заплатил, а QR снова свободен для
второй оплаты. Поэтому сверка спрашивает шлюз (lookup): SUCCESS и DECLINED переносятся как есть, «платёж неизвестен»
(NOT_FOUND) означает, что списания не было, «ещё в обработке» (PENDING) и «выяснить не удалось» (UNKNOWN) оставляют
платёж в PENDING.

Шлюз реального провайдера может отвечать NOT_FOUND сразу после отправки (данные доезжают не мгновенно): такой адаптер
задаёт атрибут not_found_grace, и по NOT_FOUND платёж закрывается только спустя RECONCILE_AFTER + not_found_grace.
У эмулятора «таймаут» означает, что банк запрос не получил, поэтому ожидание без запаса.

Сбой на одном платеже (шлюз, база) не останавливает проход: ошибка пишется, обработка идёт дальше.
"""
import logging
from datetime import timedelta

from tips.domain.errors import StateChanged
from tips.domain.qr_sign import parse_time

log = logging.getLogger("tips.jobs")

RECONCILE_AFTER = timedelta(minutes=15)  # сколько PENDING ждёт ответа шлюза до первого запроса статуса
STUCK_AFTER = timedelta(hours=24)  # PENDING или CREATED дольше этого и не удаётся закрыть: нужен человек
CREATED_TTL = timedelta(minutes=30)  # CREATED без confirm: клиент ушёл (запас на 3-D Secure дольше 15 минут из ADR-002)
BATCH = 200  # платежей за один проход: «ядовитый» платёж не должен блокировать остальные навсегда


def _note(details, key, payment_id):
    if details is not None:
        details.setdefault(key, []).append(payment_id)


def _reconcile_provider(store, provider, p, now, details):
    """Платёж API v2: статус по reference у провайдера. Возвращает итоговый статус или None, если закрывать рано."""
    from tips.adapters.provider import base as pb
    from tips.app.provider_flow import apply_provider_status

    result = provider.get_status(reference=p["payment_id"], provider_payment_id=p.get("provider_payment_id") or None)
    age = now - store.pending_since(p)
    if result.status in (pb.SUCCEEDED, pb.FAILED):
        return apply_provider_status(store, p["payment_id"], result, provider, now, "system")["status"]
    if result.status == pb.NOT_FOUND and age >= RECONCILE_AFTER + provider.not_found_grace:
        store.transition(p, "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", now)  # провайдер платёж не знает
        return "DECLINED"
    if result.status == pb.ACTION_REQUIRED and age >= CREATED_TTL:  # клиент ушёл со страницы 3-D Secure
        if provider.void(result.provider_payment_id or p.get("provider_payment_id")):
            store.transition(p, "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", now)
            return "DECLINED"
    if age >= STUCK_AFTER:
        _note(details, "stuck", p["payment_id"])
    return None


def reconcile(store, gateway, now, details=None, provider=None):
    """Закрывает PENDING старше RECONCILE_AFTER по ответу шлюза (v1) или провайдера (v2).
    Возвращает номера платежей, получивших итог."""
    finished = []
    grace = getattr(gateway, "not_found_grace", timedelta(0))
    for p in store.stale_pending(now - RECONCILE_AFTER, BATCH):
        try:
            if p.get("provider"):  # платёж API v2: спрашиваем провайдера, а не эмулятор
                if provider is None or provider.name != p["provider"]:
                    continue
                outcome = _reconcile_provider(store, provider, p, now, details)
                if outcome is not None:
                    finished.append(p["payment_id"])
                    _note(details, outcome, p["payment_id"])
                continue
            qr = store.find_qr(p["qr_id"])
            if qr is None:  # QR недоступен (другой файл в CSV-режиме): итог узнать не у кого
                continue
            state, error = gateway.lookup(p["qr_id"], qr["nonce"])
            age = now - store.pending_since(p)
            if state == "SUCCESS":
                new_status, code, action = "SUCCESS", None, "payment_confirmed"
            elif state == "DECLINED":
                new_status, code, action = "DECLINED", error or "CARD_DECLINED", "payment_declined"
            elif state == "NOT_FOUND" and age >= RECONCILE_AFTER + grace:  # шлюз платёж не получал: деньги не списаны
                new_status, code, action = "DECLINED", "GATEWAY_TIMEOUT", "payment_declined"
            else:  # в обработке, не удалось выяснить или NOT_FOUND слишком рано: закрывать нельзя
                if age >= STUCK_AFTER:
                    _note(details, "stuck", p["payment_id"])
                continue
            store.transition(p, new_status, code, action, "system", now)
        except StateChanged:  # платёж уже закрыт параллельным запросом или другой сверкой
            continue
        except Exception:  # noqa: BLE001 - один платёж не должен останавливать проход
            log.exception("сверка платежа %s не удалась", p["payment_id"])
            _note(details, "errors", p["payment_id"])
            continue
        finished.append(p["payment_id"])
        _note(details, new_status, p["payment_id"])
    return finished


def cancel_stale(store, gateway, now, ttl=CREATED_TTL, details=None):
    """Отменяет CREATED, который не подтвердили за ttl. Сначала шлюз (void), потом статус: клиент мог уже заплатить
    на странице провайдера, и отмена только у нас потеряла бы его деньги. Если void не удался, платёж остаётся CREATED
    (QR занят) и повторяется на следующем проходе; дольше суток попадает в details["stuck_created"].
    Возвращает номера отменённых платежей."""
    canceled = []
    for p in store.stale_created(now - ttl, BATCH):
        try:
            qr = store.find_qr(p["qr_id"])
            if qr is None or not gateway.void(p["qr_id"], qr["nonce"]):
                if now - parse_time(p["timestamp"]) >= STUCK_AFTER:
                    _note(details, "stuck_created", p["payment_id"])
                continue
            store.transition(p, "CANCELED", None, "payment_canceled", "system", now)
        except StateChanged:  # клиент успел подтвердить или отменить
            continue
        except Exception:  # noqa: BLE001
            log.exception("автоотмена платежа %s не удалась", p["payment_id"])
            _note(details, "errors", p["payment_id"])
            continue
        canceled.append(p["payment_id"])
    return canceled
