"""Подпись QR версии 2 (дизайн: docs/product/v2-design.md, раздел 3).

В отличие от v1 (HMAC-SHA256, 64 бита, без номера ключа, nonce 24 бита) здесь:
  * номер ключа kid в строке QR и во входе подписи: проверка одним сравнением, ротация без перебора ключей;
  * 128 бит подписи в base64url (22 символа) и 128 бит nonce;
  * вход подписи собирается с префиксами длины: поля нельзя сдвинуть и склеить иначе;
  * в подпись входят сумма чека и валюта: изменение суммы в реестре делает QR недействительным;
  * префикс домена «tips-qr-v2» исключает использование подписи v1 как подписи v2 и наоборот.
"""
import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import sys
from datetime import timedelta

from tips.domain.qr_sign import parse_time

QR_V2_TTL = timedelta(hours=4)
KID = re.compile(r"\A[a-z0-9]{1,8}\Z")
SIG_V2 = re.compile(r"\A[A-Za-z0-9_-]{22}\Z")
DOMAIN = b"tips-qr-v2"
MIN_KEY_BYTES = 32


class QrV2Keys:
    """Связка ключей: активный подписывает, все перечисленные проверяют."""

    def __init__(self, ring, active_kid):
        if active_kid not in ring:
            raise ValueError("активный kid отсутствует в связке")
        for kid, key in ring.items():
            if not KID.match(kid) or len(key) < MIN_KEY_BYTES:
                raise ValueError(f"ключ {kid}: kid a-z0-9 до 8 символов, ключ не короче {MIN_KEY_BYTES} байт")
        self.ring, self.active_kid = dict(ring), active_kid

    @classmethod
    def from_env(cls):
        """TIPS_QR_KEYS="k1:<base64>,k2:<base64>", TIPS_QR_ACTIVE_KID=k2. Ключи отдельные от TIPS_HMAC_SECRET (v1)."""
        raw, active = os.environ.get("TIPS_QR_KEYS", ""), os.environ.get("TIPS_QR_ACTIVE_KID", "")
        if not raw or not active:
            sys.exit("Ошибка: не заданы TIPS_QR_KEYS и TIPS_QR_ACTIVE_KID (ключи QR v2: python -m tips.http.keys qr)")
        ring = {}
        for item in raw.split(","):
            kid, _, b64 = item.strip().partition(":")
            try:
                ring[kid] = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                sys.exit(f"Ошибка: ключ {kid} в TIPS_QR_KEYS не base64")
        try:
            return cls(ring, active)
        except ValueError as exc:
            sys.exit(f"Ошибка: {exc}")


def new_nonce():
    return secrets.token_urlsafe(16)  # 128 бит


def _message(kid, qr, amount, currency):
    fields = [kid, qr["qr_id"], qr["merchant_id"], qr["employee_id"], qr["receipt_id"], amount, currency, qr["nonce"],
              str(int(parse_time(qr["exp"]).timestamp()))]
    parts = [DOMAIN]
    for field in fields:
        data = field.encode("utf-8")
        parts.append(len(data).to_bytes(2, "big") + data)
    return b"".join(parts)


def _sign(key, kid, qr, amount, currency):
    digest = hmac.new(key, _message(kid, qr, amount, currency), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def sign_v2(keys, qr, amount, currency="RUB"):
    """Подпись активным ключом; amount строкой «637.00»."""
    return _sign(keys.ring[keys.active_kid], keys.active_kid, qr, amount, currency)


def verify_v2(keys, qr, amount, currency, link_sig):
    """Подпись записи и подпись из ссылки должны совпасть с HMAC ключа из строки QR. Неизвестный kid: отказ."""
    if not isinstance(link_sig, str) or not link_sig.isascii() or not SIG_V2.match(link_sig):
        return False
    kid = qr.get("key_id")
    key = keys.ring.get(kid) if isinstance(kid, str) else None
    if key is None or not isinstance(qr.get("signature"), str):
        return False
    expected = _sign(key, kid, qr, amount, currency)
    return hmac.compare_digest(expected, qr["signature"]) and hmac.compare_digest(expected, link_sig)


def qr_version(qr):
    """Версия QR из строки (CSV без столбца: 1)."""
    try:
        return int(qr.get("version", 1))
    except (TypeError, ValueError):
        return 0
