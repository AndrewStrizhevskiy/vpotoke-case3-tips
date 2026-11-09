"""Платёж и деньги в виде, описанном контрактом. Деньги в JSON числом (контракт v1), внутри всегда Decimal."""
from decimal import Decimal


def money(value):
    return float(Decimal(value))


def payment_json(payment, token=None):
    body = {
        "payment_id": payment["payment_id"],
        "qr_id": payment["qr_id"],
        "status": payment["status"],
        "base_amount": money(payment["amount"]),
        "tip_amount": money(payment["tip"]),
        "amount_total": money(payment["amount_total"]),
        "currency": "RUB",
        "error_code": payment["error_code"] or None,
        "created_at": payment["timestamp"],
    }
    if token is not None:
        body["confirm_token"] = token
    return body


def qr_view_json(view):
    return {**view, "base_amount": money(view["base_amount"])}
