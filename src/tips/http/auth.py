"""Кто вызывает защищённые операции и что ему можно.

Правила доступа (authorize_*) окончательные. Источник личности временный: в M3 это заголовки X-Dev-Role,
X-Dev-Merchant, X-Dev-Employee, и только если явно включено TIPS_DEV_AUTH=1. По умолчанию защищённые
операции отвечают 401. В M4 здесь появится проверка JWT, правила доступа останутся прежними.
"""
import os
from dataclasses import dataclass
from typing import Optional

from tips.domain.errors import PaymentError

ROLES = ("merchant", "waiter", "admin")


class Unauthenticated(Exception):
    """Нет действительных учётных данных: HTTP 401."""


@dataclass(frozen=True)
class Principal:
    role: str
    merchant_id: Optional[str] = None
    employee_id: Optional[str] = None


def authenticate(headers):
    if os.environ.get("TIPS_DEV_AUTH") != "1":
        raise Unauthenticated()
    role = headers.get("x-dev-role")
    if role not in ROLES:
        raise Unauthenticated()
    return Principal(role, headers.get("x-dev-merchant"), headers.get("x-dev-employee"))


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
