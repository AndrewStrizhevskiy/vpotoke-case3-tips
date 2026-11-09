"""Параллельность и ограничения базы: то, что на CSV-файлах гарантировать нельзя.

    TIPS_HMAC_SECRET=<демо-ключ> TIPS_TEST_DATABASE_URL=postgresql://… python3 -m unittest tests.test_postgres

Без TIPS_TEST_DATABASE_URL тесты пробуют поднять встроенный PostgreSQL (пакет pgserver); если и его нет, пропускаются.
Каждый тест получает свою схему (миграции и seed), после теста схема удаляется.
"""
import os
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

try:
    import sqlalchemy  # noqa: F401
    import psycopg  # noqa: F401
    HAVE_SQL = True
except ImportError:
    HAVE_SQL = False

if HAVE_SQL:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from tips.adapters.gateway.emulator import EmulatorGateway
    from tips.adapters.sql.admin import bootstrap, drop_schema
    from tips.adapters.sql.store import SqlStore
    from tips.app.payments import cancel_payment, confirm_payment, create_payment, find_payment
    from tips.domain.errors import PaymentError
    from tips.jobs.reconcile import reconcile
    from support import database_url

NOW = datetime(2025, 11, 1, 18, 0, 0, tzinfo=timezone.utc)
SECRET = os.environ.get("TIPS_HMAC_SECRET", "").encode()


class SlowGateway:
    """Эмулятор с задержкой и счётчиком вызовов: задержка расширяет окно гонки."""

    def __init__(self, delay=0.2):
        self.inner = EmulatorGateway()
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def charge(self, qr_id, nonce):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return self.inner.charge(qr_id, nonce)


@unittest.skipUnless(HAVE_SQL and SECRET, "нужны sqlalchemy, psycopg и TIPS_HMAC_SECRET")
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.url = database_url()
        if not self.url:
            self.skipTest("нет PostgreSQL: задайте TIPS_TEST_DATABASE_URL или установите pgserver")
        self.schema = "tp_" + uuid.uuid4().hex[:12]
        self.engine = bootstrap(self.url, self.schema, seed_data=True)
        self.addCleanup(self._drop)

    def _drop(self):
        self.engine.dispose()
        drop_schema(self.url, self.schema)

    def store(self):
        return SqlStore(self.engine)

    def request(self, key, qr_id="qr_001", tip_pct=5):
        qr = self.store().find_qr(qr_id)
        receipt = qr["receipt_id"]
        base = self.store().receipt_amount(qr["merchant_id"], receipt)
        return {"qr_id": qr_id, "sig": qr["signature"], "receipt_id": receipt, "base_amount": base,
                "tip_pct": tip_pct, "idempotency_key": key}

    def race(self, jobs):
        """Запустить функции одновременно (барьер) и вернуть их результаты или исключения."""
        results = [None] * len(jobs)
        barrier = threading.Barrier(len(jobs))

        def worker(i, job):
            barrier.wait()
            try:
                results[i] = job()
            except Exception as exc:  # noqa: BLE001 - исключения тоже результат гонки
                results[i] = exc

        threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def count(self, sql, **params):
        with self.engine.connect() as c:
            return c.execute(text(sql), params).scalar()

    # ---- гонки ----

    def test_parallel_creates_with_different_keys_make_one_payment(self):
        jobs = [lambda i=i: create_payment(self.request(f"key-{i}"), SECRET, self.store(), NOW) for i in range(20)]
        results = self.race(jobs)
        created = [r for r in results if isinstance(r, tuple)]
        errors = [r.code for r in results if isinstance(r, PaymentError)]
        unexpected = [r for r in results if not isinstance(r, (tuple, PaymentError))]
        self.assertEqual(unexpected, [])
        self.assertEqual(len(created), 1)
        self.assertEqual(errors, ["PAYMENT_IN_PROGRESS"] * 19)
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 1)
        self.assertEqual(self.count("SELECT count(*) FROM audit_events"), 1)

    def test_parallel_creates_with_same_key_return_same_payment(self):
        jobs = [lambda: create_payment(self.request("same-key"), SECRET, self.store(), NOW) for _ in range(20)]
        results = self.race(jobs)
        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        self.assertEqual({r[0]["payment_id"] for r in results}, {"p_0101"})
        self.assertEqual(sum(1 for r in results if r[1] is False), 1)  # один создал, остальные повтор
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 1)

    def test_parallel_confirms_call_gateway_once(self):
        payment, _ = create_payment(self.request("k"), SECRET, self.store(), NOW)
        gateway = SlowGateway()
        jobs = [lambda: confirm_payment(find_payment(self.store(), payment["payment_id"]), self.store(), gateway, NOW)
                for _ in range(10)]
        results = self.race(jobs)
        self.assertTrue(all(isinstance(r, dict) for r in results), results)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual({r["status"] for r in results} - {"PENDING", "SUCCESS"}, set())
        self.assertEqual(find_payment(self.store(), payment["payment_id"])["status"], "SUCCESS")
        # created, submitted, confirmed: по одному событию на переход
        self.assertEqual(self.count("SELECT count(*) FROM audit_events"), 3)

    def test_cancel_and_confirm_race_ends_in_one_consistent_state(self):
        payment, _ = create_payment(self.request("k"), SECRET, self.store(), NOW)
        gateway = SlowGateway(delay=0.05)
        pid = payment["payment_id"]
        jobs = [lambda: confirm_payment(find_payment(self.store(), pid), self.store(), gateway, NOW),
                lambda: cancel_payment(find_payment(self.store(), pid), self.store(), NOW)]
        results = self.race(jobs)
        final = find_payment(self.store(), pid)["status"]
        self.assertIn(final, ("SUCCESS", "CANCELED"))
        kinds = [r.code if isinstance(r, PaymentError) else r["status"] for r in results]
        if final == "CANCELED":
            self.assertEqual(gateway.calls, 0)  # отменили до отправки в банк: банк не вызывался
        else:
            self.assertEqual(gateway.calls, 1)
            self.assertIn("CANCEL_NOT_ALLOWED", kinds)  # отмена после отправки запрещена
        statuses = self.count("SELECT count(DISTINCT status) FROM payments WHERE payment_id = :p", p=pid)
        self.assertEqual(statuses, 1)

    def test_parallel_reconcile_closes_once(self):
        payment, _ = create_payment(self.request("k", qr_id="qr_010"), SECRET, self.store(), NOW)
        confirm_payment(payment, self.store(), EmulatorGateway(), NOW)  # qr_010 -> PENDING (таймаут шлюза)
        self.assertEqual(find_payment(self.store(), payment["payment_id"])["status"], "PENDING")
        later = datetime(2025, 11, 1, 18, 15, 0, tzinfo=timezone.utc)
        results = self.race([lambda: reconcile(self.store(), later) for _ in range(8)])
        self.assertTrue(all(isinstance(r, list) for r in results), results)
        self.assertEqual(sorted(sum(results, [])), [payment["payment_id"]])
        self.assertEqual(self.count("SELECT count(*) FROM audit_events WHERE action = 'payment_declined'"), 1)

    # ---- ограничения базы без участия кода приложения ----

    def insert_payment_sql(self, pid, status, key):
        with self.engine.begin() as c:
            c.execute(text(
                "INSERT INTO payments (payment_id, qr_id, merchant_id, employee_id, amount, tip, amount_total, status, "
                "idempotency_key, created_at) SELECT :pid, qr_id, merchant_id, employee_id, 100, 5, 105, :st, :key, now() "
                "FROM qr_codes WHERE qr_id = 'qr_001'"), {"pid": pid, "st": status, "key": key})

    def test_database_rejects_second_active_payment_per_qr(self):
        self.insert_payment_sql("p_9001", "CREATED", "a")
        with self.assertRaises(IntegrityError) as ctx:
            self.insert_payment_sql("p_9002", "PENDING", "b")
        self.assertIn("payments_one_active", str(ctx.exception))

    def test_database_rejects_second_success_per_qr(self):
        self.insert_payment_sql("p_9001", "SUCCESS", "a")
        with self.assertRaises(IntegrityError) as ctx:
            self.insert_payment_sql("p_9002", "SUCCESS", "b")
        self.assertIn("payments_one_success", str(ctx.exception))

    def test_database_allows_retry_after_decline_or_cancel(self):
        self.insert_payment_sql("p_9001", "DECLINED", "a")
        self.insert_payment_sql("p_9002", "CANCELED", "b")
        self.insert_payment_sql("p_9003", "CREATED", "c")
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 3)

    def test_database_rejects_duplicate_idempotency_key_per_qr(self):
        self.insert_payment_sql("p_9001", "DECLINED", "same")
        with self.assertRaises(IntegrityError):
            self.insert_payment_sql("p_9002", "DECLINED", "same")

    def test_database_checks_amounts(self):
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as c:
                c.execute(text(
                    "INSERT INTO payments (payment_id, qr_id, merchant_id, employee_id, amount, tip, amount_total, status, "
                    "idempotency_key, created_at) SELECT 'p_9001', qr_id, merchant_id, employee_id, 100, 5, 106, 'CREATED', "
                    "'k', now() FROM qr_codes WHERE qr_id = 'qr_001'"))  # итог не равен сумме и чаевым

    def test_audit_events_are_append_only(self):
        create_payment(self.request("k"), SECRET, self.store(), NOW)
        for statement in ("UPDATE audit_events SET actor = 'officer'", "DELETE FROM audit_events",
                          "TRUNCATE audit_events"):
            with self.assertRaises(DBAPIError, msg=statement):
                with self.engine.begin() as c:
                    c.execute(text(statement))
        self.assertEqual(self.count("SELECT count(*) FROM audit_events"), 1)

    def test_seed_loaded_and_money_is_numeric(self):
        self.assertEqual(self.count("SELECT count(*) FROM qr_codes"), 100)
        self.assertEqual(self.count("SELECT count(*) FROM seed_payments"), 100)
        self.assertEqual(self.count("SELECT sum(tip) FROM seed_payments WHERE payment_id = 'p_0001'"), Decimal("31.85"))

    def test_rollback_leaves_no_partial_write(self):
        store = self.store()
        with self.assertRaises(RuntimeError):
            with store.atomic():
                store.insert_payment({"payment_id": "p_9001", "qr_id": "qr_001", "merchant_id": "m_001",
                                      "employee_id": "e_001", "amount": "100.00", "tip": "5.00", "amount_total": "105.00",
                                      "status": "CREATED", "idempotency_key": "k", "timestamp": "2025-11-01T18:00:00Z"}, NOW)
                raise RuntimeError("сбой после вставки")
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM audit_events"), 0)


if __name__ == "__main__":
    unittest.main()
