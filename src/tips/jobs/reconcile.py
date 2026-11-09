"""Сверка зависших платежей (ADR-001/002)."""
from datetime import timedelta

from tips.domain.errors import StateChanged
from tips.domain.qr_sign import parse_time

RECONCILE_AFTER = timedelta(minutes=15)


def reconcile(store, now):
    """ADR-001/002: PENDING без ответа 15 минут -> DECLINED (GATEWAY_TIMEOUT)."""
    submitted = {}
    for ev in store.audit():
        if ev["action"] == "payment_submitted":
            submitted[ev["payment_id"]] = parse_time(ev["at"])
    closed = []
    for p in store.payments():
        if p["status"] == "PENDING" and p["payment_id"] in submitted \
                and now - submitted[p["payment_id"]] >= RECONCILE_AFTER:
            try:
                store.transition(p, "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", now)
            except StateChanged:  # платёж уже закрыт параллельным запросом или другой сверкой
                continue
            closed.append(p["payment_id"])
    return closed
