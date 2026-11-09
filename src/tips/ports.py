"""Границы между логикой и внешним миром. Сервисы знают только эти интерфейсы."""
from contextlib import AbstractContextManager
from typing import Dict, List, Optional, Protocol, Tuple
from decimal import Decimal
from datetime import datetime

Row = Dict[str, str]


class Catalog(Protocol):
    """Справочники и реестр чеков (только чтение)."""

    def merchant(self, merchant_id: str) -> Optional[Row]: ...
    def merchant_status(self, merchant_id: str) -> Optional[str]: ...
    def employee(self, employee_id: str) -> Optional[Row]: ...
    def find_qr(self, qr_id: str) -> Optional[Row]: ...
    def all_qrs(self) -> List[Row]: ...
    def receipt_amount(self, merchant_id: str, receipt_id: str) -> Optional[Decimal]: ...
    def seed_payments(self) -> List[Row]: ...


class PaymentStore(Protocol):
    """Платежи и аудит. Смена статуса и событие аудита записываются вместе."""

    def atomic(self) -> AbstractContextManager:
        """Транзакция: всё внутри либо записывается целиком, либо откатывается (на CSV — без эффекта)."""
        ...

    def lock_qr(self, qr_id: str) -> None:
        """Блокировка строки QR до конца транзакции: параллельные платежи по одному QR идут по очереди."""
        ...

    def payments(self) -> List[Row]: ...
    def payments_for_qr(self, qr_id: str) -> List[Row]: ...
    def get_payment(self, payment_id: str) -> Optional[Row]: ...
    def audit(self) -> List[Row]: ...
    def next_payment_id(self) -> str: ...
    def insert_payment(self, payment: Row, now: datetime) -> None: ...
    def update_payment(self, payment: Row) -> None: ...
    def transition(self, payment: Row, new_status: str, error: Optional[str],
                   action: str, actor: str, now: datetime) -> None:
        """Смена статуса и событие аудита вместе. SQL-адаптер меняет статус условно (из прежнего) и при
        расхождении поднимает StateChanged."""
        ...


class TokenStore(Protocol):
    """Токен платежа: хранится только хеш (ADR-003)."""

    def set_confirm_token(self, payment_id: str, token_hash: bytes) -> None: ...
    def confirm_token_hash(self, payment_id: str) -> Optional[bytes]: ...


class AdminStore(Protocol):
    def set_merchant_status(self, merchant_id: str, new_status: str, reason: str,
                            actor: str, now: datetime) -> Optional[str]:
        """Меняет статус и пишет событие; возвращает прежний статус или None, если мерчанта нет."""
        ...


class QrStore(Protocol):
    """QR и чеки, выпущенные платформой."""

    def add_receipt(self, merchant_id: str, receipt_id: str, amount: str) -> None: ...
    def add_qr(self, qr: Row) -> None: ...


class Store(Catalog, PaymentStore, QrStore, TokenStore, AdminStore, Protocol):
    pass


class PaymentGateway(Protocol):
    """Платёжный шлюз: эмулятор сейчас, реальный провайдер позже."""

    def charge(self, qr_id: str, nonce: str) -> Tuple[str, Optional[str]]:
        """Исход: ("SUCCESS"|"DECLINED"|"PENDING", код ошибки или None)."""
        ...


class QrRenderer(Protocol):
    def render(self, url: str, qr_id: str) -> Optional[str]: ...
