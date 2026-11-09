"""Деньги: Decimal и округление ROUND_HALF_UP (ADR-004), float для сумм не используется."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from tips.domain.errors import PaymentError

CENT = Decimal("0.01")
MAX_BASE = Decimal("1000000")
MAX_TOTAL = Decimal("2000000")
MAX_TIP_SHARE = Decimal("0.5")


def to_money(value):
    """Сумма не больше чем с двумя знаками после запятой: лишние знаки — ошибка, а не тихое округление."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.quantize(CENT):
            raise InvalidOperation
        return amount.quantize(CENT)
    except InvalidOperation:
        raise PaymentError("VALIDATION_ERROR")


def calc_amounts(base, tip_pct=None, tip_amount=None):
    """ADR-004: Decimal + ROUND_HALF_UP, float для денег не используется."""
    if tip_amount is None:
        tip_amount = (base * Decimal(tip_pct) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return tip_amount, base + tip_amount


def fmt(value):
    return f"{value:.2f}"
