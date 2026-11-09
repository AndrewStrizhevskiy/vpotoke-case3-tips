"""Подпись QR (HMAC-SHA256, ADR-003), срок действия и разбор времени UTC (ADR-009)."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
QR_TTL = timedelta(hours=24)


def parse_time(text):
    return datetime.strptime(text, TIME_FMT).replace(tzinfo=timezone.utc)


def compute_signature(secret, qr):
    payload = "|".join([qr["merchant_id"], qr["employee_id"], qr["receipt_id"], qr["nonce"], qr["exp"]])
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:16]


def verify_signature(secret, qr, sig):
    """Q-1: подпись записи и подпись из ссылки должны совпасть с HMAC (ADR-003)."""
    expected = compute_signature(secret, qr)
    return (hmac.compare_digest(expected, qr["signature"])
            and isinstance(sig, str) and hmac.compare_digest(expected, sig))


def check_exp(qr, now):
    """Q-2: QR действует, пока exp >= now."""
    return parse_time(qr["exp"]) >= now
