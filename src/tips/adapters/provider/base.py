"""Общие типы адаптера платёжного провайдера (дизайн: docs/product/v2-design.md, раздел 5).

Статусы результата: SUCCEEDED, FAILED, PROCESSING (в обработке), ACTION_REQUIRED (нужна проверка 3-D Secure),
NOT_FOUND (провайдер платёж не знает). Ключ идемпотентности у провайдера и reference это payment_id: повтор
вызова после сбоя возвращает тот же платёж, а не списывает второй раз.
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

SUCCEEDED, FAILED, PROCESSING, ACTION_REQUIRED, NOT_FOUND = "SUCCEEDED", "FAILED", "PROCESSING", "ACTION_REQUIRED", "NOT_FOUND"


@dataclass(frozen=True)
class ProviderResult:
    status: str
    provider_payment_id: Optional[str] = None
    action_url: Optional[str] = None
    error_code: Optional[str] = None  # CARD_DECLINED, GATEWAY_TIMEOUT и т. п. (нормализует адаптер)
    reference: Optional[str] = None  # наш payment_id, как его вернул провайдер: нужен, если событие пришло раньше записи


@dataclass(frozen=True)
class RefundResult:
    status: str  # SUCCEEDED, PENDING, FAILED
    provider_refund_id: Optional[str] = None


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    event_type: str
    provider_payment_id: Optional[str]
    payload: dict


class WebhookInvalid(Exception):
    """Подпись вебхука неверна, устарела или тело повреждено: событие не принимается, причина наружу не сообщается."""


class ProviderUnavailable(Exception):
    """Провайдер не ответил: исход неизвестен, платёж остаётся в PENDING до сверки."""


class PaymentProvider:
    """Интерфейс. Реальные провайдеры и песочница реализуют те же методы; not_found_grace задаёт запас ожидания,
    если провайдер отдаёт данные с задержкой (NOT_FOUND сразу после отправки не означает «не списано»)."""

    name = "provider"
    not_found_grace = timedelta(0)
    action_hosts = ()  # допустимые узлы адреса 3-D Secure (action_url чужого узла отвергается)

    def create_payment(self, reference, amount, currency, payment_method_token, return_url, idempotency_key):
        raise NotImplementedError

    def get_status(self, reference=None, provider_payment_id=None):
        raise NotImplementedError

    def void(self, provider_payment_id):
        raise NotImplementedError

    def refund(self, provider_payment_id, amount, idempotency_key):
        raise NotImplementedError

    def parse_webhook(self, headers, raw_body, now):
        raise NotImplementedError
