"""Подпись QR (HMAC-SHA256, ADR-003), срок действия и разбор времени UTC (ADR-009)."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
QR_TTL = timedelta(hours=24)


def parse_time(text):
    return datetime.strptime(text, TIME_FMT).replace(tzinfo=timezone.utc)


class QrKeys:
    """Ключи подписи QR (ADR-009): новые QR подписываются активным, проверка принимает и выведенные из обращения.

    Ротация без простоя: новый ключ становится активным, старый остаётся в retired на 24 часа (срок жизни QR),
    затем удаляется. Поле kid в QR нет, поэтому при проверке пробуются все ключи.
    """

    def __init__(self, active, retired=()):
        self.active = active
        self.retired = tuple(retired)

    def all(self):
        return (self.active,) + self.retired


def as_keys(secret):
    return secret if isinstance(secret, QrKeys) else QrKeys(secret)


def _signature(key, qr):
    payload = "|".join([qr["merchant_id"], qr["employee_id"], qr["receipt_id"], qr["nonce"], qr["exp"]])
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:16]


def compute_signature(secret, qr):
    return _signature(as_keys(secret).active, qr)


def verify_signature(secret, qr, sig):
    """Q-1: подпись записи и подпись из ссылки должны совпасть с HMAC одного из действующих ключей (ADR-003)."""
    if not isinstance(sig, str) or not sig.isascii():  # compare_digest не принимает не-ASCII строки: это была бы ошибка 500
        return False
    ok = False
    for key in as_keys(secret).all():  # без раннего выхода: время не выдаёт, какой ключ подошёл
        expected = _signature(key, qr)
        if hmac.compare_digest(expected, qr["signature"]) and hmac.compare_digest(expected, sig):
            ok = True
    return ok


def check_exp(qr, now):
    """Q-2: QR действует, пока exp >= now."""
    return parse_time(qr["exp"]) >= now
