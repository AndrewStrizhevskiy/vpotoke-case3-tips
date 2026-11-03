#!/usr/bin/env python3
"""Read-only verification of analyst examples and seed relationships; no output CSV writes."""
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / 'prototype' / '01_данные'


def check(condition, what='проверка данных'):
    """Явная проверка: в отличие от assert, работает и под python -O."""
    if not condition:
        raise SystemExit(f'Проверка не пройдена: {what}')


def load(name):
    with (SEED / (name + '.csv')).open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def rounded(value):
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def metrics(rows):
    success = [r for r in rows if r['status'] == 'SUCCESS']
    total = sum((Decimal(r['tip']) for r in success), Decimal(0))
    percent = sum((Decimal(r['tip']) / Decimal(r['amount']) * 100 for r in success), Decimal(0))
    return dict(tips_total=rounded(total), payments_count=len(rows),
                avg_tip_pct=rounded(percent / len(success)) if success else None,
                success_rate=rounded(Decimal(len(success)) / len(rows)) if rows else 0.0)


def verify():
    merchants, employees, payments, qrs = map(load, ['merchants', 'employees', 'payments', 'qr_links'])
    for rows, key in [(merchants, 'merchant_id'), (employees, 'employee_id'),
                      (payments, 'payment_id'), (qrs, 'qr_id'), (qrs, 'nonce')]:
        check(len({r[key] for r in rows}) == len(rows), key)
    merchant = {r['merchant_id']: r for r in merchants}
    employee = {r['employee_id']: r for r in employees}
    qr = {r['qr_id']: r for r in qrs}
    check(sum(len(rows[0]) for rows in [merchants, employees, payments, qrs]) == 22)
    for row in merchants:
        check(1 <= len(row['name']) <= 100 and len(row['description']) <= 200)
        check(row['status'] in {'ACTIVE', 'BLOCKED', 'PENDING'})
    for row in employees:
        check(row['merchant_id'] in merchant)
        check(1 <= len(row['name']) <= 100)
        check(re.fullmatch(r'\*{4}[0-9]{4}', row['payment_details']))
    for row in qrs:
        check(employee[row['employee_id']]['merchant_id'] == row['merchant_id'])
        check(re.fullmatch(r'[0-9a-f]{6}', row['nonce']))
        check(re.fullmatch(r'[0-9a-f]{16}', row['signature']))
        datetime.fromisoformat(row['exp'].replace('Z', '+00:00'))
    check(len({(r['merchant_id'], r['receipt_id']) for r in qrs}) == len(qrs))
    for row in payments:
        q = qr[f"qr_{int(row['payment_id'][2:]):03d}"]
        check(row['merchant_id'] == q['merchant_id'])
        check(row['employee_id'] == q['employee_id'])
        check(row['timestamp'][:10] == q['exp'][:10])
        check(0 < Decimal(row['amount']) <= 1000000)
        check(0 <= Decimal(row['tip']) <= Decimal(row['amount']) / 2)
        check(row['status'] in {'CREATED', 'PENDING', 'SUCCESS', 'DECLINED', 'CANCELED'})
    month = [r for r in payments if '2025-11-01' <= r['timestamp'][:10] <= '2025-11-30']
    m = [r for r in month if r['merchant_id'] == 'm_001']
    e = [r for r in month if r['employee_id'] == 'e_001']
    merchant_report = dict(merchant_id='m_001', period='2025-11', **metrics(m))
    good = [r for r in e if r['status'] == 'SUCCESS']
    days = defaultdict(Decimal)
    for row in good:
        days[row['timestamp'][:10]] += Decimal(row['tip'])
    employee_report = dict(employee_id='e_001', period='2025-11',
                           tips_total=metrics(e)['tips_total'], payments_count=len(e),
                           avg_tip_amount=rounded(sum((Decimal(r['tip']) for r in good), Decimal(0)) / len(good)) if good else None,
                           top_day=min(d for d in days if days[d] == max(days.values())) if days else None)
    daily = []
    for day in sorted({r['timestamp'][:10] for r in m}):
        result = metrics([r for r in m if r['timestamp'][:10] == day])
        daily.append(dict(date=day, **{k: result[k] for k in ['tips_total', 'payments_count', 'avg_tip_pct']}))
    report = dict(merchant=merchant_report, employee=employee_report,
                  daily=dict(merchant_id='m_001', daily=daily))
    check((merchant_report['tips_total'], merchant_report['payments_count'], merchant_report['avg_tip_pct'], merchant_report['success_rate']) == (414.85, 6, 9.0, 0.83))
    check((employee_report['tips_total'], employee_report['payments_count'], employee_report['avg_tip_amount'], employee_report['top_day']) == (83.60, 2, 41.80, '2025-11-03'))
    check(daily == [dict(date='2025-11-01', tips_total=245.90, payments_count=3, avg_tip_pct=10.0),
                     dict(date='2025-11-03', tips_total=168.95, payments_count=3, avg_tip_pct=7.5)])
    check(metrics([]) == dict(tips_total=0.0, payments_count=0, avg_tip_pct=None, success_rate=0.0))
    check((Decimal('53.50') * Decimal('0.05')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == Decimal('2.68'))
    print(json.dumps(dict(row_counts={name: len(rows) for name, rows in zip(['merchants', 'employees', 'payments', 'qr_links'], [merchants, employees, payments, qrs])},
                          payment_statuses=dict(Counter(r['status'] for r in payments)), reports=report), ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    verify()
