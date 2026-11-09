"""Платёж: создание, подтверждение, отмена (ADR-002, 004, 008)."""
from decimal import Decimal

from tips.domain.errors import PaymentError
from tips.domain.money import MAX_BASE, MAX_TIP_SHARE, MAX_TOTAL, calc_amounts, fmt, to_money
from tips.domain.qr_sign import TIME_FMT, check_exp, verify_signature
from tips.domain.validation import validate_request


def find_payment(store, payment_id):
    for p in store.payments():
        if p["payment_id"] == payment_id:
            return p
    raise PaymentError("NOT_FOUND")


def create_payment(req, secret, store, now):
    key = validate_request(req)
    qr_id = req["qr_id"]
    base = to_money(req["base_amount"])
    tip_pct = req.get("tip_pct")
    tip_in = to_money(req["tip_amount"]) if "tip_amount" in req else None
    if tip_pct is not None and (isinstance(tip_pct, bool) or not isinstance(tip_pct, (int, Decimal))
                                or tip_pct != int(tip_pct)):
        raise PaymentError("VALIDATION_ERROR")

    # Q-1. Неизвестный qr_id и плохая подпись дают один ответ: перебор id ничего не раскрывает.
    qr = store.find_qr(qr_id)
    if qr is None or not verify_signature(secret, qr, req.get("sig")):
        raise PaymentError("INVALID_QR")

    # P-2 (ADR-008). Повтор ключа раньше проверок exp/nonce: иначе повтор после SUCCESS
    # получил бы QR_USED вместо уже созданного платежа.
    existing = [p for p in store.payments() if p["qr_id"] == qr_id]
    for p in existing:
        if p["idempotency_key"] == key:
            same = p["amount"] == fmt(base) and req["receipt_id"] == qr["receipt_id"] and (
                tip_in == Decimal(p["tip"]) if tip_in is not None
                else Decimal(p["tip"]) == calc_amounts(base, tip_pct)[0])
            if not same:
                raise PaymentError("IDEMPOTENCY_CONFLICT")
            return p, True

    # Q-2 .. Q-4
    if not check_exp(qr, now):
        raise PaymentError("QR_EXPIRED")
    if any(p["status"] == "SUCCESS" for p in existing):  # nonce гасится на SUCCESS (ADR-002)
        raise PaymentError("QR_USED")
    if store.merchant_status(qr["merchant_id"]) != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    if req["receipt_id"] != qr["receipt_id"]:  # Q-5: чек из запроса должен быть чеком этого QR
        raise PaymentError("RECEIPT_MISMATCH")

    # Сумма чека — серверная правда (ADR-003), затем P-1, Q-6, P-3
    receipt = store.receipt_amount(qr["merchant_id"], qr["receipt_id"])
    if receipt is None:
        raise PaymentError("NOT_FOUND")
    if base != receipt:
        raise PaymentError("AMOUNT_MISMATCH")
    if not (0 < base <= MAX_BASE):
        raise PaymentError("AMOUNT_OUT_OF_RANGE")
    if tip_pct is not None and not (0 <= tip_pct <= 50):
        raise PaymentError("TIP_OUT_OF_RANGE")
    if tip_in is not None and not (0 <= tip_in <= base * MAX_TIP_SHARE):
        raise PaymentError("TIP_OUT_OF_RANGE")
    tip, total = calc_amounts(base, tip_pct, tip_in)
    if total > MAX_TOTAL:
        raise PaymentError("AMOUNT_OUT_OF_RANGE")

    # Блокировка QR, пока есть платёж в CREATED или PENDING (ADR-002)
    if any(p["status"] in ("CREATED", "PENDING") for p in existing):
        raise PaymentError("PAYMENT_IN_PROGRESS")

    payment = {
        "payment_id": store.next_payment_id(),
        "merchant_id": qr["merchant_id"],
        "employee_id": qr["employee_id"],
        "amount": fmt(base),
        "tip": fmt(tip),
        "status": "CREATED",
        "timestamp": now.strftime(TIME_FMT),
        "qr_id": qr_id,
        "idempotency_key": key,
        "amount_total": fmt(total),
        "error_code": "",
    }
    store.insert_payment(payment, now)
    return payment, False


def confirm_payment(payment, store, gateway, now):
    status = payment["status"]
    if status == "CANCELED":  # подтверждать нечего (ADR-002)
        raise PaymentError("PAYMENT_STATE_CONFLICT")
    if status != "CREATED":  # повтор: текущий статус и прежний код, шлюз не вызывается
        return payment
    qr = store.find_qr(payment["qr_id"])
    if qr is None:  # QR выпущен в другом файле: укажите тот же --qr-file, что при создании
        raise PaymentError("NOT_FOUND")
    store.transition(payment, "PENDING", None, "payment_submitted", "client", now)
    result, error = gateway.charge(payment["qr_id"], qr["nonce"])
    if result == "SUCCESS":
        store.transition(payment, "SUCCESS", None, "payment_confirmed", "system", now)
    elif result == "DECLINED":
        store.transition(payment, "DECLINED", error, "payment_declined", "system", now)
    else:  # таймаут: остаёмся в PENDING, ждём сверку, перехода нет — аудит не пишем
        payment["error_code"] = error
        store.update_payment(payment)
    return payment


def cancel_payment(payment, store, now):
    if payment["status"] == "CANCELED":  # повтор отмены идемпотентен (ADR-002)
        return payment
    if payment["status"] != "CREATED":
        raise PaymentError("CANCEL_NOT_ALLOWED")
    store.transition(payment, "CANCELED", None, "payment_canceled", "client", now)
    return payment
