"""Вход, обновление и проверка токенов (M4).

access: JWT ES256 на 15 минут, роль и привязка к заведению зашиты в claims, но при каждом запросе сверяются с
пользователем в базе (закрытый или переведённый на другую роль пользователь теряет доступ сразу, не через 15 минут).
refresh: случайный непрозрачный токен, в базе только SHA-256. Каждое обновление выдаёт новый токен, а старый
помечается использованным. Повторное предъявление использованного токена означает кражу: вся цепочка отзывается.
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from tips.adapters.jwt_keys import AUDIENCE, ISSUER
from tips.domain.errors import PaymentError, Unauthenticated
from tips.domain.qr_sign import parse_time

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=14)
FAMILY_MAX_AGE = timedelta(days=30)  # без нового входа цепочка обновлений дольше не живёт
REUSE_GRACE = timedelta(seconds=10)  # повторная отправка того же запроса обновления не считается кражей
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
LOGIN_MAX = 100
PASSWORD_MAX = 256


@dataclass(frozen=True)
class Principal:
    role: str
    merchant_id: Optional[str] = None
    employee_id: Optional[str] = None
    user_id: Optional[str] = None  # кто именно: нужен журналам действий администратора


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def _issue(store, keyring, user, family_id, family_started, now):
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": user["user_id"], "role": user["role"],
              "iat": int(now.timestamp()), "exp": int((now + ACCESS_TTL).timestamp()), "jti": uuid.uuid4().hex}
    for key in ("merchant_id", "employee_id"):
        if user.get(key):
            claims[key] = user[key]
    refresh = secrets.token_urlsafe(32)
    expires = min(now + REFRESH_TTL, family_started + FAMILY_MAX_AGE)
    store.insert_refresh_token(user["user_id"], family_id, _hash(refresh), now, expires, family_started)
    return {"access_token": keyring.sign(claims), "refresh_token": refresh, "token_type": "Bearer",
            "expires_in": int(ACCESS_TTL.total_seconds())}


def login(store, keyring, passwords, login_name, password, now):
    if not isinstance(login_name, str) or not isinstance(password, str):
        raise PaymentError("VALIDATION_ERROR")
    if not login_name or not password or len(login_name) > LOGIN_MAX or len(password) > PASSWORD_MAX:
        raise PaymentError("VALIDATION_ERROR")
    user = store.user_by_login(login_name.strip().lower())
    if user is None:
        passwords.burn(password)  # время ответа не должно выдавать, есть ли такой логин
        store.auth_event(None, "login_failed", now)  # видно перебор логинов; сам введённый логин не записывается
        raise Unauthenticated()
    locked = user["locked_until"] is not None and parse_time(user["locked_until"]) > now
    if locked or user["status"] != "ACTIVE":
        passwords.burn(password)
        raise Unauthenticated()
    if not passwords.verify(user["password_hash"], password):
        store.record_login_failure(user["user_id"], now, MAX_FAILURES, LOCK_SECONDS)
        raise Unauthenticated()
    with store.atomic():
        # пароль проверялся без блокировки: если за это время его сменили, закрыли учётную запись или заблокировали,
        # входа нет (иначе вход по старому паролю пережил бы его смену, а обновление хеша откатило бы новый пароль)
        fresh = store.lock_user(user["user_id"])
        if (fresh is None or fresh["password_hash"] != user["password_hash"] or fresh["status"] != "ACTIVE"
                or (fresh["locked_until"] is not None and parse_time(fresh["locked_until"]) > now)):
            raise Unauthenticated()
        store.record_login_success(fresh["user_id"], now)
        if passwords.needs_rehash(fresh["password_hash"]):  # параметры хеширования ужесточили: обновляем при входе
            store.set_password_hash(fresh["user_id"], passwords.hash(password), now, audit=False)
        return _issue(store, keyring, fresh, str(uuid.uuid4()), now, now)


def refresh(store, keyring, token, now):
    if not isinstance(token, str) or not 16 <= len(token) <= 200:
        raise Unauthenticated()
    result = None
    owner = store.refresh_token_owner(_hash(token))
    if owner is None:
        raise Unauthenticated()
    with store.atomic():  # отзыв цепочки должен записаться, даже когда ответом будет отказ
        store.lock_user(owner)  # сначала пользователь, потом токен: тот же порядок, что при смене пароля и закрытии
        row = store.refresh_token_for_update(_hash(token))
        if row is None or row["revoked"]:
            pass
        elif row["rotated"]:
            if now - parse_time(row["rotated_at"]) > REUSE_GRACE:
                store.revoke_family(row["family_id"], now)
                store.auth_event(row["user_id"], "refresh_reuse", now)
        elif parse_time(row["expires_at"]) > now:
            user = store.user_by_id(row["user_id"])
            if user is None or user["status"] != "ACTIVE":
                store.revoke_family(row["family_id"], now)
            else:
                store.mark_refresh_rotated(row["token_id"], now)
                result = _issue(store, keyring, user, row["family_id"], parse_time(row["family_started_at"]), now)
                store.auth_event(user["user_id"], "refresh_ok", now)
    if result is None:
        raise Unauthenticated()
    return result


def logout(store, token, now):
    """Отзывает всю цепочку токена. Ответ всегда одинаков: нельзя проверять, чей токен у вас в руках."""
    if not isinstance(token, str) or not 16 <= len(token) <= 200:
        return
    owner = store.refresh_token_owner(_hash(token))
    if owner is None:
        return
    with store.atomic():
        store.lock_user(owner)
        row = store.refresh_token_for_update(_hash(token))
        if row is not None:
            store.revoke_family(row["family_id"], now)
            store.auth_event(row["user_id"], "logout", now)


def authenticate(store, keyring, access_token, now):
    """Кто делает запрос. Любая неудача (подпись, срок, закрытая учётная запись, сменившаяся роль) — Unauthenticated."""
    claims = keyring.verify(access_token, now)
    user = store.user_by_id(claims["sub"])
    if user is None or user["status"] != "ACTIVE":
        raise Unauthenticated()
    if (claims.get("role") != user["role"] or claims.get("merchant_id") != user["merchant_id"]
            or claims.get("employee_id") != user["employee_id"]):
        raise Unauthenticated()
    if claims["iat"] < int(parse_time(user["password_changed_at"]).timestamp()):  # пароль сменили после выдачи токена
        raise Unauthenticated()
    return Principal(user["role"], user["merchant_id"], user["employee_id"], user["user_id"])
