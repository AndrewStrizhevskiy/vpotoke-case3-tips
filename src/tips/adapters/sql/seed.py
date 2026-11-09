"""Загрузка seed-данных организаторов в базу (для разработки и тестов, не часть миграций)."""
import csv
import os

from sqlalchemy import text


def _read(data_dir, name):
    with open(os.path.join(data_dir, name), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def seed(engine, data_dir):
    """Справочники, чеки, QR и история платежей. Сумма чека берётся по правилу p_000N <-> qr_00N (ADR-003)."""
    merchants = _read(data_dir, "merchants.csv")
    employees = _read(data_dir, "employees.csv")
    qrs = _read(data_dir, "qr_links.csv")
    payments = _read(data_dir, "payments.csv")
    by_payment = {p["payment_id"]: p for p in payments}
    receipts = []
    for q in qrs:
        pay = by_payment.get("p_" + q["qr_id"][len("qr_"):].zfill(4))
        if pay is not None:
            receipts.append({"m": q["merchant_id"], "r": q["receipt_id"], "a": pay["amount"]})
    with engine.begin() as c:
        c.execute(text("INSERT INTO merchants (merchant_id, name, status, description) "
                       "VALUES (:merchant_id, :name, :status, :description)"), merchants)
        c.execute(text("INSERT INTO employees (employee_id, merchant_id, name, payout_mask) "
                       "VALUES (:employee_id, :merchant_id, :name, :payment_details)"), employees)
        c.execute(text("INSERT INTO receipts (merchant_id, receipt_id, amount) VALUES (:m, :r, :a)"), receipts)
        c.execute(text(
            "INSERT INTO qr_codes (qr_id, merchant_id, employee_id, receipt_id, nonce, exp, signature, created_at) "
            "VALUES (:qr_id, :merchant_id, :employee_id, :receipt_id, :nonce, CAST(:exp AS timestamptz), :signature, "
            "CAST(:exp AS timestamptz) - interval '24 hours')"), qrs)
        c.execute(text(
            "INSERT INTO seed_payments (payment_id, merchant_id, employee_id, amount, tip, status, created_at) "
            "VALUES (:payment_id, :merchant_id, :employee_id, :amount, :tip, :status, "
            "CAST(:timestamp AS timestamptz))"), payments)


def add_extra_qrs(engine, csv_path, receipt_amount="500.00"):
    """Тестовые QR из отдельного файла (например, мерчант BLOCKED): для них заводится чек и запись QR."""
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    with engine.begin() as c:
        for r in rows:
            c.execute(text("INSERT INTO receipts (merchant_id, receipt_id, amount) VALUES (:m, :r, :a) "
                           "ON CONFLICT DO NOTHING"),
                      {"m": r["merchant_id"], "r": r["receipt_id"], "a": receipt_amount})
            c.execute(text(
                "INSERT INTO qr_codes (qr_id, merchant_id, employee_id, receipt_id, nonce, exp, signature, created_at) "
                "VALUES (:qr_id, :merchant_id, :employee_id, :receipt_id, :nonce, CAST(:exp AS timestamptz), :signature, "
                "CAST(:exp AS timestamptz) - interval '24 hours')"), r)


def remove_extra_qrs(engine, csv_path):
    """Убрать тестовые QR и их чеки после теста (если по ним нет платежей)."""
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    with engine.begin() as c:
        for r in rows:
            c.execute(text("DELETE FROM qr_codes WHERE qr_id = :qr_id"), r)
            c.execute(text("DELETE FROM receipts WHERE merchant_id = :merchant_id AND receipt_id = :receipt_id"), r)
