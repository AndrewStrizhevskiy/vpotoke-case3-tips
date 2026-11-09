"""Отчёты по мерчанту, официанту и по дням (ADR-007)."""
from tips.domain.errors import PaymentError
from tips.domain.reports import report_metrics
from tips.domain.validation import parse_day


def build_report(store, merchant_id, employee_id, from_text, to_text, daily):
    """Отчёт по мерчанту, официанту или по дням. Payment = seed payments.csv + payments_out.csv."""
    if bool(merchant_id) == bool(employee_id) or (daily and not merchant_id):
        raise PaymentError("VALIDATION_ERROR")
    first, last = parse_day(from_text), parse_day(to_text)
    if first > last:
        raise PaymentError("VALIDATION_ERROR")
    if merchant_id and store.merchant_status(merchant_id) is None:
        raise PaymentError("NOT_FOUND")
    if employee_id and not store.employee(employee_id):
        raise PaymentError("NOT_FOUND")
    key, owner = ("merchant_id", merchant_id) if merchant_id else ("employee_id", employee_id)
    rows = [r for r in store.seed_payments() + store.payments()
            if r[key] == owner and first.isoformat() <= r["timestamp"][:10] <= last.isoformat()]
    if daily:
        days = sorted({r["timestamp"][:10] for r in rows})
        items = []
        for day in days:
            m = report_metrics([r for r in rows if r["timestamp"][:10] == day])
            items.append({"date": day, "tips_total": m["tips_total"],
                          "payments_count": m["payments_count"], "avg_tip_pct": m["avg_tip_pct"]})
        return {"merchant_id": merchant_id, "daily": items}
    period = first.strftime("%Y-%m")
    if period != last.strftime("%Y-%m"):  # формат period для произвольного интервала в API не определён
        raise PaymentError("VALIDATION_ERROR")
    m = report_metrics(rows)
    if merchant_id:
        return {"merchant_id": merchant_id, "period": period, **{k: m[k] for k in
                ("tips_total", "payments_count", "avg_tip_pct", "success_rate")}}
    return {"employee_id": employee_id, "period": period, **{k: m[k] for k in
            ("tips_total", "payments_count", "avg_tip_amount", "top_day")}}
