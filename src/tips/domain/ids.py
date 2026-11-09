"""Непрозрачные идентификаторы API v2: префикс и 128 случайных бит в base32 Crockford (26 символов, нижний регистр).

Не порядковые и не содержат времени (в отличие от ULID и UUIDv7), поэтому по ним нельзя перебирать чужие чеки и
платежи или оценивать нагрузку. От идентификаторов v1 (qr_001, p_0101) отличаются длиной и алфавитом.
"""
import re
import secrets

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # без i, l, o, u
BODY = r"[0-9a-hjkmnp-tv-z]{26}"
QR_V2 = re.compile(rf"\Aqr_{BODY}\Z")  # \Z, а не $: «$» пропускает перевод строки в конце
PAYMENT_V2 = re.compile(rf"\Apay_{BODY}\Z")
REFUND_V2 = re.compile(rf"\Arf_{BODY}\Z")
IDEMPOTENCY_KEY_V2 = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def new_id(prefix):
    number = int.from_bytes(secrets.token_bytes(16), "big")  # 128 бит
    digits = []
    for _ in range(26):
        digits.append(ALPHABET[number & 31])
        number >>= 5
    return f"{prefix}_{''.join(reversed(digits))}"
