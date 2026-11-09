"""Метрики отчётов (ADR-007): считаются из платежей, отдельной таблицы нет."""
from decimal import Decimal, ROUND_HALF_UP

from tips.domain.money import CENT


def round_money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def report_metrics(rows):
    """Чаевые и средние — только по SUCCESS, количество попыток и доля успеха — по всем."""
    success = [r for r in rows if r["status"] == "SUCCESS"]
    tips = sum((Decimal(r["tip"]) for r in success), Decimal(0))
    percent = sum((Decimal(r["tip"]) / Decimal(r["amount"]) * 100 for r in success), Decimal(0))
    by_day = {}
    for r in success:
        by_day[r["timestamp"][:10]] = by_day.get(r["timestamp"][:10], Decimal(0)) + Decimal(r["tip"])
    best = max(by_day.values()) if by_day else None
    return {
        "tips_total": round_money(tips),
        "payments_count": len(rows),
        "avg_tip_pct": round_money(percent / len(success)) if success else None,
        "avg_tip_amount": round_money(tips / len(success)) if success else None,
        "success_rate": round_money(Decimal(len(success)) / len(rows)) if rows else 0.0,
        "top_day": min(d for d in by_day if by_day[d] == best) if by_day else None,
    }
