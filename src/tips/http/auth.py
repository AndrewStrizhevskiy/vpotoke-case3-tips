"""Что можно делать каждой роли. Кто вызывает, определяет app/auth.py (JWT и пользователь в базе).

Правила доступа (authorize_*) не зависят от способа входа: принимают Principal и идентификаторы из запроса.
Принцип: владелец ресурса берётся из проверенного токена, а не из параметров запроса.
"""
from tips.app.auth import Principal
from tips.domain.errors import PaymentError

__all__ = ["Principal", "authorize_issue_qr", "authorize_report", "authorize_admin"]


def authorize_issue_qr(principal, merchant_id, employee_id):
    """Мерчант выпускает QR для своего заведения, официант только на себя; администратор QR не выпускает."""
    if principal.role == "merchant" and principal.merchant_id == merchant_id:
        return
    if principal.role == "waiter" and principal.merchant_id == merchant_id and principal.employee_id == employee_id:
        return
    raise PaymentError("ACCESS_DENIED")


def authorize_report(principal, merchant_id, employee_id):
    """Мерчант видит только свой отчёт, официант только свой (employee_id из токена), администратор всё."""
    if principal.role == "admin":
        return
    if merchant_id is not None and principal.role == "merchant" and principal.merchant_id == merchant_id:
        return
    if employee_id is not None and principal.role == "waiter" and principal.employee_id == employee_id:
        return
    raise PaymentError("ACCESS_DENIED")


def authorize_admin(principal):
    if principal.role != "admin":
        raise PaymentError("ACCESS_DENIED")
