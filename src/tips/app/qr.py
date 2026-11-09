"""Выпуск QR на чек (FR-1): проверки, nonce, срок, подпись, платёжная ссылка."""
import secrets

from tips.config import PAY_URL
from tips.domain.errors import PaymentError
from tips.domain.money import MAX_BASE, fmt
from tips.domain.qr_sign import QR_TTL, TIME_FMT, compute_signature, parse_time
from tips.domain.validation import validate_qr_request


def create_qr(req, secret, store, now, renderer=None):
    """Выпуск QR на чек. Проверки: мерчант ACTIVE, официант этого мерчанта, чек не меняет сумму."""
    amount = validate_qr_request(req)
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
    if not (0 < amount <= MAX_BASE):
        raise PaymentError("AMOUNT_OUT_OF_RANGE")
    known = store.receipt_amount(merchant_id, receipt_id)
    if known is not None and known != amount:
        raise PaymentError("AMOUNT_MISMATCH")

    rows = store.all_qrs()
    for row in rows:  # на чек уже есть действующий QR — отдаём его, а не выпускаем второй
        if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id \
                and parse_time(row["exp"]) >= now:
            if row["employee_id"] != employee_id:
                raise PaymentError("ACCESS_DENIED")
            return qr_result(row, new=False, renderer=renderer)

    last = max((int(r["qr_id"][3:]) for r in rows), default=0)
    qr = {"qr_id": f"qr_{last + 1:03d}", "merchant_id": merchant_id, "employee_id": employee_id,
          "receipt_id": receipt_id, "nonce": secrets.token_hex(3),
          "exp": (now + QR_TTL).strftime(TIME_FMT)}
    qr["signature"] = compute_signature(secret, qr)
    if known is None:  # касса передала новый чек
        store.add_receipt(merchant_id, receipt_id, fmt(amount))
    store.add_qr(qr)
    return qr_result(qr, new=True, renderer=renderer)


def qr_result(qr, new, renderer=None):
    """Платёжная ссылка с подписью (ADR-003) и, если задан и доступен рендерер, PNG с QR-кодом."""
    url = f"{PAY_URL}?qr={qr['qr_id']}&sig={qr['signature']}"
    png = renderer.render(url, qr["qr_id"]) if renderer else None
    return {"qr_id": qr["qr_id"], "pay_url": url, "exp": qr["exp"], "new": new, "png": png}
