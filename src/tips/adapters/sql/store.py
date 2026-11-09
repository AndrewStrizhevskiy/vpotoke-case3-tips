"""PostgreSQL-хранилище: те же методы, что у CsvStore, но с транзакциями и ограничениями базы.

Строки возвращаются в том же виде, что в CSV (словари из строк), поэтому предметная логика не меняется.
Защита от двойной оплаты держится не только на проверках в коде: уникальные индексы
payments_one_active и payments_one_success срабатывают, даже если два запроса проскочили проверки.
"""
import json
import os
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tips.domain.errors import PaymentError, StateChanged
from tips.domain.qr_sign import TIME_FMT, parse_time

# двоеточия в формате экранированы: text() иначе принимает «:MI» за параметр запроса
TS = 'to_char({col} AT TIME ZONE \'UTC\', \'YYYY-MM-DD"T"HH24\\:MI\\:SS"Z"\')'

PAYMENT_COLS = (
    "payment_id, merchant_id, employee_id, amount::text AS amount, tip::text AS tip, status, "
    + TS.format(col="created_at") + " AS timestamp, qr_id, idempotency_key, amount_total::text AS amount_total, "
    "coalesce(error_code, '') AS error_code, api_version, coalesce(provider, '') AS provider, "
    "coalesce(provider_payment_id, '') AS provider_payment_id, next_action"
)
QR_COLS = ("qr_id, merchant_id, employee_id, receipt_id, nonce, " + TS.format(col="exp") + " AS exp, signature, "
           "version, key_id")

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

    def live_qr_for_receipt(self, merchant_id, receipt_id, now, version=1):
        return self._one(f"SELECT {QR_COLS} FROM qr_codes WHERE merchant_id = :m AND receipt_id = :r "
                         "AND version = :v AND exp >= CAST(:now AS timestamptz) ORDER BY qr_id LIMIT 1",
                         m=merchant_id, r=receipt_id, v=version, now=now.strftime(TIME_FMT))

    def lock_receipt(self, merchant_id, receipt_id):
        self._run("SELECT 1 FROM receipts WHERE merchant_id = :m AND receipt_id = :r FOR UPDATE", m=merchant_id, r=receipt_id)

    def seed_payments(self):
        return self._run("SELECT payment_id, merchant_id, employee_id, amount::text AS amount, tip::text AS tip, status, "
                         + TS.format(col="created_at") + " AS timestamp FROM seed_payments ORDER BY payment_id")

    # ---- платежи и аудит ----

    def lock_qr(self, qr_id):
        self._run("SELECT 1 FROM qr_codes WHERE qr_id = :id FOR UPDATE", id=qr_id)

    def payments(self):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments ORDER BY payment_id")

    def stale_pending(self, cutoff, limit=200):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments WHERE status = 'PENDING' "
                         "AND submitted_at <= CAST(:c AS timestamptz) ORDER BY submitted_at, payment_id LIMIT :n",
                         c=cutoff.strftime(TIME_FMT), n=limit)

    def pending_since(self, payment):
        row = self._one("SELECT " + TS.format(col="submitted_at") + " AS at FROM payments WHERE payment_id = :id",
                        id=payment["payment_id"])
        return parse_time(row["at"])

    def stale_created(self, cutoff, limit=200):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments WHERE status = 'CREATED' "
                         "AND created_at <= CAST(:c AS timestamptz) ORDER BY created_at, payment_id LIMIT :n",
                         c=cutoff.strftime(TIME_FMT), n=limit)

    def payments_for_qr(self, qr_id):
        return self._run(f"SELECT {PAYMENT_COLS} FROM payments WHERE qr_id = :id ORDER BY payment_id", id=qr_id)

    def get_payment(self, payment_id):
        return self._one(f"SELECT {PAYMENT_COLS} FROM payments WHERE payment_id = :id", id=payment_id)

    def audit(self):
        rows = self._run(
            "SELECT event_id AS number, payment_id, qr_id, action, "
            "coalesce(from_status, '') AS from_status, to_status, amount_total::text AS amount_total, actor, "
            + TS.format(col="at") + " AS at FROM audit_events ORDER BY event_id")
        for row in rows:
            row["event_id"] = f"evt_{row.pop('number'):04d}"
        return rows

    def next_payment_id(self):
        number = self._one("SELECT nextval('payment_seq') AS n")["n"]
        return f"p_{number:04d}"  # форматирование в Python: lpad в SQL обрезает числа длиннее ширины

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
                    "status, idempotency_key, created_at, api_version) VALUES (:pid, :qr, :m, :e, :amount, :tip, :total, "
                    ":status, :key, CAST(:ts AS timestamptz), :api)",
                    pid=payment["payment_id"], qr=payment["qr_id"], m=payment["merchant_id"], e=payment["employee_id"],
                    amount=payment["amount"], tip=payment["tip"], total=payment["amount_total"], status=payment["status"],
                    key=payment["idempotency_key"], ts=payment["timestamp"], api=payment.get("api_version", 1))
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
                "finalized_at = CASE WHEN :new IN ('SUCCESS', 'DECLINED', 'CANCELED', 'REFUNDED') THEN CAST(:ts AS timestamptz) "
                "ELSE finalized_at END WHERE payment_id = :id AND status = :old",
                new=new_status, err=error or "", ts=stamp, id=payment["payment_id"], old=old)
            if changed == 0:
                raise StateChanged(payment["payment_id"])
            payment["status"] = new_status
            payment["error_code"] = error or ""
            self._audit(payment, action, old, actor, now)

    def ping(self):
        return self._one("SELECT 1 AS ok")["ok"] == 1

    # ---- провайдер: поля платежа, входящие события, возвраты (API v2) ----

    def set_provider_fields(self, payment_id, provider, provider_payment_id, next_action, provider_status=None):
        self._run("UPDATE payments SET provider = COALESCE(:prov, provider), "
                  "provider_payment_id = COALESCE(:ppid, provider_payment_id), next_action = CAST(:na AS jsonb), "
                  "provider_status = COALESCE(:ps, provider_status) WHERE payment_id = :id",
                  prov=provider, ppid=provider_payment_id, na=json.dumps(next_action) if next_action else None,
                  ps=provider_status, id=payment_id)

    def payment_by_provider_ref(self, provider, provider_payment_id):
        return self._one(f"SELECT {PAYMENT_COLS} FROM payments WHERE provider = :p AND provider_payment_id = :i",
                         p=provider, i=provider_payment_id)

    def inbox_add(self, provider, event, now):
        """Принять входящее событие провайдера; повтор того же события не добавляется (False)."""
        return self._run(
            "INSERT INTO gateway_events (provider, event_id, event_type, provider_payment_id, payload, received_at) "
            "VALUES (:p, :e, :t, :i, CAST(:payload AS jsonb), CAST(:at AS timestamptz)) ON CONFLICT DO NOTHING",
            p=provider, e=event.event_id, t=event.event_type, i=event.provider_payment_id,
            payload=json.dumps(event.payload), at=now.strftime(TIME_FMT)) == 1

    def inbox_pending(self, limit=100, max_attempts=10):
        return self._run("SELECT provider, event_id, event_type, provider_payment_id, payload FROM gateway_events "
                         "WHERE processed_at IS NULL AND attempts < :m ORDER BY received_at, event_id LIMIT :n",
                         m=max_attempts, n=limit)

    def inbox_mark(self, provider, event_id, now, error=None):
        if error is None:
            self._run("UPDATE gateway_events SET processed_at = CAST(:at AS timestamptz) "
                      "WHERE provider = :p AND event_id = :e",
                      at=now.strftime(TIME_FMT), p=provider, e=event_id)
        else:
            self._run("UPDATE gateway_events SET attempts = attempts + 1, last_error = :err "
                      "WHERE provider = :p AND event_id = :e", err=error[:300], p=provider, e=event_id)

    def refund_create(self, refund_id, payment, now):
        stamp = now.strftime(TIME_FMT)
        self._run("INSERT INTO refunds (refund_id, payment_id, amount, reason, status, created_at, updated_at) "
                  "VALUES (:r, :p, :a, 'DOUBLE_PAYMENT', 'REQUIRED', CAST(:t AS timestamptz), CAST(:t AS timestamptz)) "
                  "ON CONFLICT DO NOTHING", r=refund_id, p=payment["payment_id"], a=payment["amount_total"], t=stamp)

    def refunds_open(self, limit=50, max_attempts=5):
        return self._run("SELECT r.refund_id, r.payment_id, r.amount::text AS amount, r.attempts, p.provider_payment_id, "
                         "p.provider FROM refunds r JOIN payments p USING (payment_id) "
                         "WHERE r.status IN ('REQUIRED', 'PENDING') AND r.attempts < :m ORDER BY r.created_at LIMIT :n",
                         m=max_attempts, n=limit)

    def refunds_failed(self, max_attempts=5):
        return self._run("SELECT refund_id, payment_id FROM refunds WHERE status IN ('REQUIRED', 'PENDING', 'FAILED') "
                         "AND attempts >= :m", m=max_attempts)

    def refund_mark(self, refund_id, status, now, provider_refund_id=None, attempt=False):
        self._run("UPDATE refunds SET status = :s, provider_refund_id = COALESCE(:prid, provider_refund_id), "
                  "attempts = attempts + :a, updated_at = CAST(:t AS timestamptz) WHERE refund_id = :r",
                  s=status, prid=provider_refund_id, a=1 if attempt else 0, t=now.strftime(TIME_FMT), r=refund_id)

    def metrics_snapshot(self, now):
        """Счётчики для /metrics: платежи по статусам, возраст зависших, очереди событий и возвратов."""
        stamp = now.strftime(TIME_FMT)
        by = {r["status"]: int(r["n"]) for r in self._run("SELECT status, count(*) AS n FROM payments GROUP BY status")}

        def age(table_filter, col):
            row = self._one(f"SELECT COALESCE(EXTRACT(EPOCH FROM (CAST(:t AS timestamptz) - min({col}))), 0)::bigint AS s "
                            f"FROM payments WHERE {table_filter}", t=stamp)
            return max(0, int(row["s"]))

        count = lambda sql: int(self._one(sql)["n"])  # noqa: E731
        return {
            "payments": by,
            "oldest_pending": age("status = 'PENDING'", "submitted_at"),
            "oldest_created": age("status = 'CREATED'", "created_at"),
            "inbox_backlog": count("SELECT count(*) AS n FROM gateway_events WHERE processed_at IS NULL AND attempts < 10"),
            "inbox_stuck": count("SELECT count(*) AS n FROM gateway_events WHERE processed_at IS NULL AND attempts >= 10"),
            "refunds_open": count("SELECT count(*) AS n FROM refunds WHERE status IN ('REQUIRED', 'PENDING') AND attempts < 5"),
            "refunds_stuck": count("SELECT count(*) AS n FROM refunds WHERE status IN ('REQUIRED', 'PENDING', 'FAILED') "
                                   "AND attempts >= 5"),
        }

    # ---- лимиты запросов ----

    def hit(self, bucket, window_seconds, now):
        """Учесть запрос в текущем фиксированном окне; возвращает число запросов в нём (включая этот)."""
        start = int(now.timestamp()) // window_seconds * window_seconds
        row = self._one(
            "INSERT INTO rate_hits (bucket, window_start, hits) VALUES (:b, to_timestamp(:w), 1) "
            "ON CONFLICT (bucket, window_start) DO UPDATE SET hits = rate_hits.hits + 1 RETURNING hits",
            b=bucket, w=start)
        return row["hits"]

    def hits(self, bucket, window_seconds, now):
        """Сколько запросов уже учтено в текущем окне (без учёта нового)."""
        start = int(now.timestamp()) // window_seconds * window_seconds
        row = self._one("SELECT hits FROM rate_hits WHERE bucket = :b AND window_start = to_timestamp(:w)", b=bucket, w=start)
        return row["hits"] if row else 0

    def purge_rate_hits(self, before):
        return self._run("DELETE FROM rate_hits WHERE window_start < CAST(:t AS timestamptz)", t=before.strftime(TIME_FMT))

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

    # ---- пользователи, токены обновления, журнал входа ----

    USER_COLS = ("user_id, login, password_hash, role, merchant_id, employee_id, status, failed_attempts, "
                 + TS.format(col="locked_until") + " AS locked_until, "
                 + TS.format(col="password_changed_at") + " AS password_changed_at")

    def create_user(self, user, now):
        stamp = now.strftime(TIME_FMT)
        try:
            with self.atomic():
                self._run(
                    "INSERT INTO users (user_id, login, password_hash, role, merchant_id, employee_id, created_at, "
                    "password_changed_at) VALUES (:id, :login, :hash, :role, :m, :e, CAST(:at AS timestamptz), "
                    "CAST(:at AS timestamptz))",
                    id=user["user_id"], login=user["login"], hash=user["password_hash"], role=user["role"],
                    m=user.get("merchant_id"), e=user.get("employee_id"), at=stamp)
                self.auth_event(user["user_id"], "user_created", now)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or "ограничение"
            raise ValueError(f"пользователь не создан: нарушено {constraint} "
                             "(логин занят или у официанта уже есть учётная запись)") from exc

    def user_by_login(self, login):
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE login = :login", login=login)

    def user_by_id(self, user_id):
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE user_id = :id", id=user_id)

    def lock_user(self, user_id):
        """Строка пользователя под блокировкой до конца транзакции. Порядок блокировок везде один: пользователь, затем
        его токены (иначе смена пароля и обновление токена могли бы взаимно заблокироваться)."""
        return self._one(f"SELECT {self.USER_COLS} FROM users WHERE user_id = :id FOR UPDATE", id=user_id)

    def count_active_admins(self):
        self._run("SELECT pg_advisory_xact_lock(7283001)")  # закрытия администраторов идут по одному
        return self._one("SELECT count(*) AS n FROM users WHERE role = 'admin' AND status = 'ACTIVE'")["n"]

    def record_login_failure(self, user_id, now, max_failures, lock_seconds):
        """Счётчик растёт атомарно и затухает: неудача старше окна блокировки начинает отсчёт заново.
        На пороге учётная запись блокируется, и счётчик обнуляется."""
        with self.atomic():
            row = self._one(
                "UPDATE users SET failed_attempts = CASE WHEN last_failed_at IS NOT NULL AND last_failed_at > "
                "CAST(:now AS timestamptz) - make_interval(secs => :secs) THEN failed_attempts + 1 ELSE 1 END, "
                "last_failed_at = CAST(:now AS timestamptz) WHERE user_id = :id RETURNING failed_attempts",
                id=user_id, now=now.strftime(TIME_FMT), secs=lock_seconds)
            locked = bool(row) and row["failed_attempts"] >= max_failures
            if locked:
                self._run("UPDATE users SET failed_attempts = 0, "
                          "locked_until = CAST(:now AS timestamptz) + make_interval(secs => :secs) WHERE user_id = :id",
                          id=user_id, now=now.strftime(TIME_FMT), secs=lock_seconds)
            self.auth_event(user_id, "login_locked" if locked else "login_failed", now)
            return locked

    def record_login_success(self, user_id, now):
        with self.atomic():
            self._run("UPDATE users SET failed_attempts = 0, last_failed_at = NULL, locked_until = NULL "
                      "WHERE user_id = :id", id=user_id)
            self.auth_event(user_id, "login_ok", now)

    def set_password_hash(self, user_id, password_hash, now, audit=True):
        with self.atomic():
            self._run("UPDATE users SET password_hash = :h WHERE user_id = :id", h=password_hash, id=user_id)
            if audit:  # смена пароля, а не обновление параметров хеша при входе
                self._run("UPDATE users SET password_changed_at = CAST(:at AS timestamptz), failed_attempts = 0, "
                          "last_failed_at = NULL, locked_until = NULL WHERE user_id = :id",
                          at=now.strftime(TIME_FMT), id=user_id)
                self.auth_event(user_id, "password_changed", now)

    def set_user_status(self, user_id, status, now):
        with self.atomic():
            self._run("UPDATE users SET status = :s WHERE user_id = :id", s=status, id=user_id)
            self.auth_event(user_id, "user_disabled" if status == "DISABLED" else "user_enabled", now)

    def insert_refresh_token(self, user_id, family_id, token_hash, now, expires_at, family_started_at):
        self._run(
            "INSERT INTO refresh_tokens (user_id, family_id, token_hash, issued_at, expires_at, family_started_at) "
            "VALUES (:u, CAST(:f AS uuid), :h, CAST(:now AS timestamptz), CAST(:exp AS timestamptz), "
            "CAST(:started AS timestamptz))",
            u=user_id, f=family_id, h=token_hash, now=now.strftime(TIME_FMT), exp=expires_at.strftime(TIME_FMT),
            started=family_started_at.strftime(TIME_FMT))

    def refresh_token_owner(self, token_hash):
        """Чей токен, без блокировки: нужно, чтобы сначала заблокировать пользователя, потом токен."""
        row = self._one("SELECT user_id FROM refresh_tokens WHERE token_hash = :h", h=token_hash)
        return row["user_id"] if row else None

    def refresh_token_for_update(self, token_hash):
        return self._one(
            "SELECT token_id, user_id, family_id::text AS family_id, " + TS.format(col="expires_at") + " AS expires_at, "
            + TS.format(col="family_started_at") + " AS family_started_at, "
            "rotated_at IS NOT NULL AS rotated, revoked_at IS NOT NULL AS revoked, "
            + TS.format(col="rotated_at") + " AS rotated_at "
            "FROM refresh_tokens WHERE token_hash = :h FOR UPDATE", h=token_hash)

    def mark_refresh_rotated(self, token_id, now):
        self._run("UPDATE refresh_tokens SET rotated_at = CAST(:now AS timestamptz) WHERE token_id = :id",
                  id=token_id, now=now.strftime(TIME_FMT))

    def revoke_family(self, family_id, now):
        self._run("UPDATE refresh_tokens SET revoked_at = CAST(:now AS timestamptz) "
                  "WHERE family_id = CAST(:f AS uuid) AND revoked_at IS NULL", f=family_id, now=now.strftime(TIME_FMT))

    def revoke_user_tokens(self, user_id, now):
        self._run("UPDATE refresh_tokens SET revoked_at = CAST(:now AS timestamptz) "
                  "WHERE user_id = :u AND revoked_at IS NULL", u=user_id, now=now.strftime(TIME_FMT))

    def auth_event(self, user_id, action, now):
        self._run("INSERT INTO auth_events (user_id, action, at) VALUES (:u, :a, CAST(:at AS timestamptz))",
                  u=user_id, a=action, at=now.strftime(TIME_FMT))

    # ---- QR и чеки, выпущенные платформой ----

    def add_receipt(self, merchant_id, receipt_id, amount):
        self._run("INSERT INTO receipts (merchant_id, receipt_id, amount, currency) VALUES (:m, :r, :a, 'RUB') "
                  "ON CONFLICT DO NOTHING", m=merchant_id, r=receipt_id, a=amount)

    def next_qr_id(self):
        """Следующий номер из последовательности базы; занятые (например, заведённые вручную) пропускаются."""
        for _ in range(100):
            number = self._one("SELECT nextval('qr_seq') AS n")["n"]
            qr_id = f"qr_{number:03d}"
            if self.find_qr(qr_id) is None:
                return qr_id
        raise RuntimeError("не удалось подобрать свободный номер QR")

    def add_qr(self, qr):
        self._run(
            "INSERT INTO qr_codes (qr_id, merchant_id, employee_id, receipt_id, nonce, exp, signature, version, key_id, "
            "created_at) VALUES (:qr, :m, :e, :r, :nonce, CAST(:exp AS timestamptz), :sig, :v, :kid, "
            "CAST(:exp AS timestamptz) - interval '24 hours')",
            qr=qr["qr_id"], m=qr["merchant_id"], e=qr["employee_id"], r=qr["receipt_id"], nonce=qr["nonce"],
            exp=qr["exp"], sig=qr["signature"], v=qr.get("version", 1), kid=qr.get("key_id"))
