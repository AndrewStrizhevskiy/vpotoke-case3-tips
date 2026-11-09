"""Токен платежа (ADR-003): 128 случайных бит, в базе только SHA-256, сравнение за постоянное время."""
import hashlib
import hmac
import secrets


def new_token():
    return secrets.token_hex(16)  # 32 символа, как требует контракт (ConfirmRequest.confirm_token)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def token_matches(token, stored_hash):
    """Токен подходит платежу; без токена или без сохранённого хеша ответ всегда «нет»."""
    if not isinstance(token, str) or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)
