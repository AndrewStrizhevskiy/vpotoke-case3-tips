"""Ключи подписи JWT: ES256, идентификатор ключа kid, ротация без простоя (ADR-009).

Подписываем активным ключом, проверяем любым действующим по kid. Алгоритм зафиксирован явно: токены с alg=none,
HS256 (подмена асимметричного ключа общим секретом) и любым другим алгоритмом отвергаются до проверки подписи.
Сроки exp и iat проверяются здесь по переданному времени, а не по часам библиотеки: время внедряется (ADR-009).
"""
import json
import os
import sys
from datetime import timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tips.domain.errors import Unauthenticated

ALGORITHM = "ES256"
ISSUER = "tips"
AUDIENCE = "tips-api"
REQUIRED = ["exp", "iat", "iss", "aud", "sub", "jti"]
LEEWAY = timedelta(seconds=5)


def generate_key_pem():
    """Новая пара ключей: (закрытый PEM, открытый PEM)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


class Keyring:
    def __init__(self, active_kid, private_pem, retired_public_pems=None):
        self.active_kid = active_kid
        self._private = serialization.load_pem_private_key(private_pem.encode(), password=None)
        if not isinstance(self._private, ec.EllipticCurvePrivateKey) or self._private.curve.name != "secp256r1":
            raise ValueError("ожидается закрытый ключ ECDSA на кривой P-256")
        self._public = {active_kid: self._private.public_key()}
        for kid, pem in (retired_public_pems or {}).items():
            if kid == active_kid:
                raise ValueError("kid выведенного из обращения ключа совпадает с активным")
            self._public[kid] = serialization.load_pem_public_key(pem.encode())

    @classmethod
    def from_env(cls):
        """TIPS_JWT_KID, TIPS_JWT_PRIVATE_KEY (PEM), по желанию TIPS_JWT_RETIRED_PUBLIC_KEYS: JSON {kid: PEM}."""
        kid, pem = os.environ.get("TIPS_JWT_KID"), os.environ.get("TIPS_JWT_PRIVATE_KEY")
        if not kid or not pem:
            sys.exit("Ошибка: не заданы TIPS_JWT_KID и TIPS_JWT_PRIVATE_KEY (ключ: python -m tips.http.keys)")
        retired = json.loads(os.environ.get("TIPS_JWT_RETIRED_PUBLIC_KEYS") or "{}")
        return cls(kid, pem.replace("\\n", "\n"), retired)

    def sign(self, claims):
        return jwt.encode(claims, self._private, algorithm=ALGORITHM, headers={"kid": self.active_kid})

    def verify(self, token, now):
        """Проверенные claims или Unauthenticated; причина наружу не сообщается."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != ALGORITHM or header.get("kid") not in self._public:
                raise Unauthenticated()
            claims = jwt.decode(
                token, self._public[header["kid"]], algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
                options={"require": REQUIRED, "verify_exp": False, "verify_iat": False, "verify_nbf": False})
        except (jwt.PyJWTError, ValueError, TypeError):
            raise Unauthenticated()
        stamp = now.timestamp()
        try:
            if not (claims["exp"] + LEEWAY.total_seconds() > stamp >= claims["iat"] - LEEWAY.total_seconds()):
                raise Unauthenticated()
        except TypeError:
            raise Unauthenticated()
        if not isinstance(claims["sub"], str) or not isinstance(claims["jti"], str):
            raise Unauthenticated()
        return claims
