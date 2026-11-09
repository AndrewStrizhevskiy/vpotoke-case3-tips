"""Хранилище на CSV: payments_out.csv, audit_out.csv, qr_out.csv, receipts_out.csv (ADR-005)."""
import csv
import os
from decimal import Decimal

from tips.config import DATA_DIR

SEED_FIELDS = ["payment_id", "merchant_id", "employee_id", "amount", "tip", "status", "timestamp"]
OUT_FIELDS = SEED_FIELDS + ["qr_id", "idempotency_key", "amount_total", "error_code"]
QR_FIELDS = ["qr_id", "merchant_id", "employee_id", "receipt_id", "nonce", "exp", "signature"]
RECEIPT_FIELDS = ["merchant_id", "receipt_id", "amount", "currency"]
AUDIT_FIELDS = ["event_id", "payment_id", "qr_id", "action", "from_status", "to_status",
                "amount_total", "actor", "at"]

TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class CsvStore:
    """Хранилище на CSV: seed-данные только читаются, результаты пишутся в *_out.csv."""

    def __init__(self, out_dir, data_dir=DATA_DIR, qr_file=None):
        self.data_dir = data_dir
        self.qr_file = qr_file or os.path.join(data_dir, "qr_links.csv")
        self.payments_path = os.path.join(out_dir, "payments_out.csv")
        self.audit_path = os.path.join(out_dir, "audit_out.csv")
        self.qr_path = os.path.join(out_dir, "qr_out.csv")
        self.receipts_path = os.path.join(out_dir, "receipts_out.csv")
        self.png_dir = os.path.join(out_dir, "qr_png")

    def qrs(self):
        return read_csv(self.qr_path) if os.path.exists(self.qr_path) else []

    def receipts(self):
        return read_csv(self.receipts_path) if os.path.exists(self.receipts_path) else []

    @staticmethod
    def append_row(path, fields, row):
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new_file:
                w.writeheader()
            w.writerow(row)

    def payments(self):
        return read_csv(self.payments_path) if os.path.exists(self.payments_path) else []

    def audit(self):
        return read_csv(self.audit_path) if os.path.exists(self.audit_path) else []

    def next_payment_id(self):
        """ADR-008: последовательно, после seed (p_0100) — с p_0101."""
        last = 100
        for row in self.payments():
            last = max(last, int(row["payment_id"][2:]))
        return f"p_{last + 1:04d}"

    def save_payments(self, rows):
        """Перезапись через временный файл: файл не остаётся наполовину записанным."""
        tmp = self.payments_path + ".part"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, self.payments_path)

    def add_audit(self, payment, action, from_status, actor, now):
        rows = self.audit()
        new_file = not os.path.exists(self.audit_path)
        with open(self.audit_path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({
                "event_id": f"evt_{len(rows) + 1:04d}",
                "payment_id": payment["payment_id"],
                "qr_id": payment["qr_id"],
                "action": action,
                "from_status": from_status or "",
                "to_status": payment["status"],
                "amount_total": payment["amount_total"],
                "actor": actor,
                "at": now.strftime(TIME_FMT),
            })

    def transition(self, payment, new_status, error, action, actor, now):
        """Смена статуса и аудит — вместе (шаг 8 ADR-004)."""
        old = payment["status"]
        payment["status"] = new_status
        payment["error_code"] = error or ""
        rows = [payment if r["payment_id"] == payment["payment_id"] else r for r in self.payments()]
        self.save_payments(rows)
        self.add_audit(payment, action, old, actor, now)
    # ---- справочники и реестр чеков (Catalog) ----

    def merchant_status(self, merchant_id):
        for row in read_csv(os.path.join(self.data_dir, "merchants.csv")):
            if row["merchant_id"] == merchant_id:
                return row["status"]
        return None

    def employee(self, employee_id):
        for row in read_csv(os.path.join(self.data_dir, "employees.csv")):
            if row["employee_id"] == employee_id:
                return row
        return None

    def find_qr(self, qr_id):
        """QR из qr_file (seed), затем выпущенные прототипом (qr_out.csv)."""
        for row in read_csv(self.qr_file):
            if row["qr_id"] == qr_id:
                return row
        return next((r for r in self.qrs() if r["qr_id"] == qr_id), None)

    def all_qrs(self):
        return read_csv(self.qr_file) + self.qrs()

    def seed_payments(self):
        return read_csv(os.path.join(self.data_dir, "payments.csv"))

    def receipt_amount(self, merchant_id, receipt_id):
        """ADR-003: сумма чека из реестра: сначала чеки, добавленные при выпуске QR, затем seed."""
        for row in self.receipts():
            if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id:
                return Decimal(row["amount"])
        for row in read_csv(os.path.join(self.data_dir, "qr_links.csv")):
            if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id:
                return self._seed_receipt_amount(row["qr_id"])
        return None

    def _seed_receipt_amount(self, qr_id):
        """Реестр чеков прототипа — payments.csv, p_000N <-> qr_00N, amount = сумма чека."""
        payment_id = "p_" + qr_id[len("qr_"):].zfill(4)
        for row in self.seed_payments():
            if row["payment_id"] == payment_id:
                return Decimal(row["amount"])
        return None

    # ---- запись ----

    def insert_payment(self, payment, now):
        self.save_payments(self.payments() + [payment])
        self.add_audit(payment, "payment_created", None, "client", now)

    def update_payment(self, payment):
        self.save_payments([payment if r["payment_id"] == payment["payment_id"] else r
                            for r in self.payments()])

    def add_receipt(self, merchant_id, receipt_id, amount):
        self.append_row(self.receipts_path, RECEIPT_FIELDS,
                        {"merchant_id": merchant_id, "receipt_id": receipt_id, "amount": amount, "currency": "RUB"})

    def add_qr(self, qr):
        self.append_row(self.qr_path, QR_FIELDS, qr)
