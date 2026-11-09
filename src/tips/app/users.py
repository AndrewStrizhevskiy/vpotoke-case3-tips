"""Учётные записи: создание, смена пароля, закрытие. Вызываются администратором платформы из командной строки."""
import re
import secrets

from tips.adapters.passwords import MAX_LENGTH, MIN_LENGTH, normalize

LOGIN_RE = re.compile(r"^[a-z0-9._@-]{3,64}$")
REPEATED = re.compile(r"^(.+?)\1+$")  # «passwordpassword», «abcabcabcabc»
COMMON = ("password", "qwerty", "123456", "letmein", "admin", "welcome", "iloveyou", "пароль", "йцукен")
ROLES = ("merchant", "waiter", "admin")


class UserError(ValueError):
    """Ошибка ввода администратора: текст показывается ему как есть."""


def check_password(login, password):
    if not isinstance(password, str) or not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise UserError(f"пароль должен быть от {MIN_LENGTH} до {MAX_LENGTH} символов")
    lowered = normalize(password).lower()
    if (len(login) >= 4 and login in lowered) or len(set(password)) < 5 or REPEATED.match(password):
        raise UserError("пароль слишком простой: не должен содержать логин, повторяться или состоять из нескольких символов")
    if any(word in lowered for word in COMMON):
        raise UserError("пароль слишком простой: содержит распространённое слово")


def create_user(store, passwords, login, password, role, merchant_id, employee_id, now):
    login = (login or "").strip().lower()
    if not LOGIN_RE.match(login):
        raise UserError("логин: 3-64 символа из a-z, 0-9, точка, дефис, подчёркивание, @")
    check_password(login, password)
    if role not in ROLES:
        raise UserError("роль: merchant, waiter или admin")
    if role == "admin" and (merchant_id or employee_id):
        raise UserError("у администратора нет заведения и официанта")
    if role == "merchant" and (not merchant_id or employee_id):
        raise UserError("у мерчанта нужен merchant_id и не нужен employee_id")
    if role == "waiter":
        if not merchant_id or not employee_id:
            raise UserError("у официанта нужны merchant_id и employee_id")
        employee = store.employee(employee_id)
        if employee is None or employee["merchant_id"] != merchant_id:
            raise UserError("такого официанта нет у этого заведения")
    if merchant_id and store.merchant(merchant_id) is None:
        raise UserError("такого заведения нет")
    user = {"user_id": "u_" + secrets.token_hex(8), "login": login, "password_hash": passwords.hash(password),
            "role": role, "merchant_id": merchant_id, "employee_id": employee_id}
    store.create_user(user, now)
    return user["user_id"]


def set_password(store, passwords, login, password, now):
    user = store.user_by_login((login or "").strip().lower())
    if user is None:
        raise UserError("такого пользователя нет")
    check_password(user["login"], password)
    new_hash = passwords.hash(password)
    with store.atomic():
        store.lock_user(user["user_id"])  # параллельные вход и обновление токена дождутся и не пропустят отзыв
        store.set_password_hash(user["user_id"], new_hash, now)
        store.revoke_user_tokens(user["user_id"], now)  # после смены пароля все входы нужно пройти заново


def disable_user(store, login, now):
    user = store.user_by_login((login or "").strip().lower())
    if user is None:
        raise UserError("такого пользователя нет")
    with store.atomic():
        if user["role"] == "admin" and user["status"] == "ACTIVE" and store.count_active_admins() <= 1:
            raise UserError("это последний действующий администратор: сначала заведите другого")
        store.lock_user(user["user_id"])
        store.set_user_status(user["user_id"], "DISABLED", now)
        store.revoke_user_tokens(user["user_id"], now)


def enable_user(store, login, now):
    user = store.user_by_login((login or "").strip().lower())
    if user is None:
        raise UserError("такого пользователя нет")
    with store.atomic():
        store.lock_user(user["user_id"])
        store.set_user_status(user["user_id"], "ACTIVE", now)
