"""PostgreSQL-хранилище: те же методы, что у CsvStore, но с транзакциями и ограничениями базы.

Строки возвращаются в том же виде, что в CSV (словари из строк), поэтому предметная логика не меняется.
Защита от двойной оплаты держится не только на проверках в коде: уникальные индексы
payments_one_active и payments_one_success срабатывают, даже если два запроса проскочили проверки.
"""
import os
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tips.domain.errors import PaymentError, StateChanged
from tips.domain.qr_sign import TIME_FMT

# двоеточия в формате экранированы: text() иначе принимает «:MI» за параметр запроса
TS = 'to_char({col} AT TIME ZONE \'UTC\', \'YYYY-MM-DD"T"HH24\\:MI\\:SS"Z"\')'

PAYMENT_COLS = (
    "payment_id, merchant_id, employee_id, amount::text AS amount, tip::text AS tip, status, "
    + TS.format(col="created_at") + " AS timestamp, qr_id, idempotency_key, amount_total::text AS amount_total, "
    "coalesce(error_code, '') AS error_code"
)
QR_COLS = "qr_id, merchant_id, employee_id, receipt_id, nonce, " + TS.format(col="exp") + " AS exp, signature"

# уникальные индексы -> код ошибки, который видит клиент
CONSTRAINT_ERRORS = {
    "payments_one_active": "PAYMENT_IN_PROGRESS",
    "payments_one_success": "QR_USED",
}


class SqlStore:
    def __init__(self, engine, out_dir=None):
        self.engine = engine
        self.png_dir = os.path.join(out_dir or ".", "qr_png")
        self._conn = None

    # ---- транзакции и запросы ----

    @contextmanager
    def atomic(self):
        if self._conn is not None:  # вложенный вызов присоединяется к внешней транзакции
            yield
            return
        with self.engine.begin() as conn:
            self._conn = conn
            try:
                yield
            finally:
                self._conn = None

    def _run(self, sql, **params):
        """Выполнить запрос; вне atomic() каждая операция — своя короткая транзакция."""
        if self._conn is not None:
            return self._finish(self._conn.execute(text(sql), params))
        with self.engine.begin() as conn:
            return self._finish(conn.execute(text(sql), params))

    @staticmethod
    def _finish(result):
        if result.returns_rows:
            return [dict(r) for r in result.mappings().all()]
        return result.rowcount

    def _one(self, sql, **params):
        rows = self._run(sql, **params)
        return rows[0] if rows else None

    # ---- справочники и реестр чеков ----

    def merchant(self, merchant_id):
        return self._one("SELECT merchant_id, name, status, description FROM merchants WHERE merchant_id = :id",
                         id=merchant_id)

    def merchant_status(self, merchant_id):
        row = self._one("SELECT status FROM merchants WHERE merchant_id = :id", id=merchant_id)
        return row["status"] if row else None

    def employee(self, employee_id):
        return self._one("SELECT employee_id, merchant_id, name, payout_mask AS payment_details "
                         "FROM employees WHERE employee_id = :id", id=employee_id)

    def find_qr(self, qr_id):
        return self._one(f"SELECT {QR_COLS} FROM qr_codes WHERE qr_id = :id", id=qr_id)

    def all_qrs(self):
        return self._run(f"SELECT {QR_COLS} FROM qr_codes ORDER BY qr_id")

    def receipt_amount(self, merchant_id, receipt_id):
        row = self._one("SELECT amount::text AS amount FROM receipts "
                        "WHERE merchant_id = :m AND receipt_id = :r", m=merchant_id, r=receipt_id)
        return Decimal(row["amount"]) if row else None

    def seed_payments(self):
        return self._run("SELECT payment_id, merchant_id, employee_id, amount::text AS amount, tip::text AS tip, status, "
                         + TS.format(col="created_at") + " AS timestamp FROM seed_payments ORDER BY payment_id")

    # ---- платежи и аудит ----

    def lock_qr(self, qr_id):
        self._run("SELECT 1 FROM qr_codes WHERE qr_id = :id FOR UPDATE", id=qr_id)

    def payments(self):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments ORDER BY payment_id")

    def payments_for_qr(self, qr_id):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments WHERE qr_id = :id ORDER BY payment_id", id=qr_id)

    def get_payment(self, payment_id):
        return self._one(f"SELECT {PAYMENT_COLS} FROM payments WHERE payment_id = :id", id=payment_id)

    def audit(self):
        return self._run(
            "SELECT 'evt_' || lpad(event_id::text, 4, '0') AS event_id, payment_id, qr_id, action, "
            "coalesce(from_status, '') AS from_status, to_status, amount_total::text AS amount_total, actor, "
            + TS.format(col="at") + " AS at FROM audit_events ORDER BY event_id")

    def next_payment_id(self):
        return self._one("SELECT 'p_' || lpad(nextval('payment_seq')::text, 4, '0') AS id")["id"]

    def _audit(self, payment, action, from_status, actor, now):
        self._run(
            "INSERT INTO audit_events (payment_id, qr_id, action, from_status, to_status, amount_total, actor, at) "
            "VALUES (:pid, :qr, :action, :from_status, :to_status, :total, :actor, CAST(:at AS timestamptz))",
            pid=payment["payment_id"], qr=payment["qr_id"], action=action, from_status=from_status,
            to_status=payment["status"], total=payment["amount_total"], actor=actor, at=now.strftime(TIME_FMT))

    def insert_payment(self, payment, now):
        try:
            with self.atomic():
                self._run(
                    "INSERT INTO payments (payment_id, qr_id, merchant_id, employee_id, amount, tip, amount_total, "
                    "status, idempotency_key, created_at) VALUES (:pid, :qr, :m, :e, :amount, :tip, :total, :status, :key, "
                    "CAST(:ts AS timestamptz))",
                    pid=payment["payment_id"], qr=payment["qr_id"], m=payment["merchant_id"], e=payment["employee_id"],
                    amount=payment["amount"], tip=payment["tip"], total=payment["amount_total"], status=payment["status"],
                    key=payment["idempotency_key"], ts=payment["timestamp"])
                self._audit(payment, "payment_created", None, "client", now)
        except IntegrityError as exc:
            code = CONSTRAINT_ERRORS.get(getattr(getattr(exc.orig, "diag", None), "constraint_name", None))
            if code:
                raise PaymentError(code) from exc
            raise

    def update_payment(self, payment):
        self._run("UPDATE payments SET error_code = NULLIF(:err, '') WHERE payment_id = :id",
                  err=payment["error_code"], id=payment["payment_id"])

    def transition(self, payment, new_status, error, action, actor, now):
        """Условное обновление: статус меняется, только если он ещё прежний. Иначе StateChanged."""
        old = payment["status"]
        stamp = now.strftime(TIME_FMT)
        with self.atomic():
            changed = self._run(
                "UPDATE payments SET status = :new, error_code = NULLIF(:err, ''), version = version + 1, "
                "submitted_at = CASE WHEN :new = 'PENDING' THEN CAST(:ts AS timestamptz) ELSE submitted_at END, "
                "finalized_at = CASE WHEN :new IN ('SUCCESS', 'DECLINED', 'CANCELED') THEN CAST(:ts AS timestamptz) "
                "ELSE finalized_at END WHERE payment_id = :id AND status = :old",
                new=new_status, err=error or "", ts=stamp, id=payment["payment_id"], old=old)
            if changed == 0:
                raise StateChanged(payment["payment_id"])
            payment["status"] = new_status
            payment["error_code"] = error or ""
            self._audit(payment, action, old, actor, now)

    # ---- токен платежа и администрирование ----

    def set_confirm_token(self, payment_id, token_hash):
        self._run("UPDATE payments SET confirm_token_hash = :h WHERE payment_id = :id", h=token_hash, id=payment_id)

    def confirm_token_hash(self, payment_id):
        row = self._one("SELECT confirm_token_hash FROM payments WHERE payment_id = :id", id=payment_id)
        value = row["confirm_token_hash"] if row else None
        return bytes(value) if value is not None else None

    def set_merchant_status(self, merchant_id, new_status, reason, actor, now):
        with self.atomic():
            row = self._one("SELECT status FROM merchants WHERE merchant_id = :id FOR UPDATE", id=merchant_id)
            if row is None:
                return None
            old = row["status"]
            self._run("UPDATE merchants SET status = :s WHERE merchant_id = :id", s=new_status, id=merchant_id)
            self._run("INSERT INTO merchant_status_events (merchant_id, old_status, new_status, reason, actor, at) "
                      "VALUES (:m, :old, :new, :reason, :actor, CAST(:at AS timestamptz))",
                      m=merchant_id, old=old, new=new_status, reason=reason, actor=actor, at=now.strftime(TIME_FMT))
            return old

    # ---- QR и чеки, выпущенные платформой ----

    def add_receipt(self, merchant_id, receipt_id, amount):
        self._run("INSERT INTO receipts (merchant_id, receipt_id, amount, currency) VALUES (:m, :r, :a, 'RUB')",
                  m=merchant_id, r=receipt_id, a=amount)

    def add_qr(self, qr):
        self._run(
            "INSERT INTO qr_codes (qr_id, merchant_id, employee_id, receipt_id, nonce, exp, signature, created_at) "
            "VALUES (:qr, :m, :e, :r, :nonce, CAST(:exp AS timestamptz), :sig, "
            "CAST(:exp AS timestamptz) - interval '24 hours')",
            qr=qr["qr_id"], m=qr["merchant_id"], e=qr["employee_id"], r=qr["receipt_id"], nonce=qr["nonce"],
            exp=qr["exp"], sig=qr["signature"])
