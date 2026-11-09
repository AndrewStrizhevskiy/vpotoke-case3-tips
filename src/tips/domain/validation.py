"""Формат запросов (шаг 1 порядка проверок, ADR-004)."""
import re
import uuid
from datetime import datetime

from tips.domain.errors import PaymentError
from tips.domain.money import to_money


def validate_request(req):
    """Шаг 1: формат запроса. tip_pct и tip_amount — ровно одно из двух."""
    if not isinstance(req, dict):
        raise PaymentError("VALIDATION_ERROR")
    if not isinstance(req.get("qr_id"), str) or not re.fullmatch(r"qr_\d{3,}", req["qr_id"]):
        raise PaymentError("VALIDATION_ERROR")
    if not isinstance(req.get("receipt_id"), str) or not re.fullmatch(r"r_\d{4}", req["receipt_id"]):
        raise PaymentError("VALIDATION_ERROR")
    if "base_amount" not in req or isinstance(req["base_amount"], bool):
        raise PaymentError("VALIDATION_ERROR")
    if ("tip_pct" in req) == ("tip_amount" in req):
        raise PaymentError("VALIDATION_ERROR")
    key = req.get("idempotency_key")
    if key is None:
        key = str(uuid.uuid4())  # CLI-удобство: клиент в реальной системе шлёт ключ сам
    if not isinstance(key, str) or not key:
        raise PaymentError("VALIDATION_ERROR")
    return key


def validate_qr_request(req):
    """Формат запроса на выпуск QR; сумма чека приходит от кассы (в прототипе — из запроса)."""
    if not isinstance(req, dict):
        raise PaymentError("VALIDATION_ERROR")
    for key, pattern in (("merchant_id", r"m_\d{3}"), ("employee_id", r"e_\d{3}"), ("receipt_id", r"r_\d{4}")):
        if not isinstance(req.get(key), str) or not re.fullmatch(pattern, req[key]):
            raise PaymentError("VALIDATION_ERROR")
    if "amount" not in req or isinstance(req["amount"], bool):
        raise PaymentError("VALIDATION_ERROR")
    return to_money(req["amount"])


def parse_day(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise PaymentError("VALIDATION_ERROR")
