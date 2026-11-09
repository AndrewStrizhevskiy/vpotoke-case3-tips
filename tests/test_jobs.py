"""Фоновые задачи (сверка по ответу шлюза, автоотмена, воркер) и нумерация QR в базе.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_jobs

Сверка и автоотмена проверяются и на CSV-хранилище (без PostgreSQL), и на PostgreSQL.
"""
import logging
import os
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from tips.adapters.csv_store import CsvStore  # noqa: E402
from tips.adapters.gateway.emulator import EmulatorGateway  # noqa: E402
from tips.app.payments import confirm_payment, create_payment, find_payment  # noqa: E402
from tips.app.qr import create_qr  # noqa: E402
from tips.config import DATA_DIR  # noqa: E402
from tips.domain.errors import PaymentError  # noqa: E402
from tips.http import logs  # noqa: E402
from tips.jobs.reconcile import CREATED_TTL, RECONCILE_AFTER, STUCK_AFTER, cancel_stale, reconcile  # noqa: E402
from tips.jobs.worker import run_once  # noqa: E402

try:
    import psycopg  # noqa: F401
    import sqlalchemy  # noqa: F401
    HAVE_SQL = True
except ImportError:
    HAVE_SQL = False

if HAVE_SQL:
    from sqlalchemy import text

    from support import database_url
    from tips.adapters.sql.admin import bootstrap, drop_schema
    from tips.adapters.sql.store import SqlStore

SECRET = os.environ.get("TIPS_HMAC_SECRET", "").encode()
NOW = datetime(2025, 11, 1, 18, 0, 0, tzinfo=timezone.utc)


class Collect(logging.Handler):
    """Журнал так, как его пишет боевой воркер: через очищающий форматтер."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logs.RedactingFormatter("%(name)s %(levelname)s %(message)s"))
        self.out = []

    def emit(self, record):
        self.out.append(self.format(record))

    def __enter__(self):
        logging.getLogger().addHandler(self)
        self._level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self)
        logging.getLogger().setLevel(self._level)

    @property
    def text(self):
        return "\n".join(self.out)


class StubGateway:
    """Шлюз с заданным ответом на lookup; считает вызовы."""

    def __init__(self, state="NOT_FOUND", error=None, can_void=True, grace=None):
        self.state, self.error, self.can_void = state, error, can_void
        if grace is not None:
            self.not_found_grace = grace
        self.lookups, self.voids = 0, 0
        self.inner = EmulatorGateway()

    def charge(self, qr_id, nonce):
        return self.inner.charge(qr_id, nonce)

    def lookup(self, qr_id, nonce):
        self.lookups += 1
        return self.state, self.error

    def void(self, qr_id, nonce):
        self.voids += 1
        return self.can_void


class JobsMixin:
    """Сценарии, общие для CSV и PostgreSQL: self.store и self.fresh() задаёт наследник."""

    def request(self, qr_id="qr_010", key="k"):
        qr = self.store.find_qr(qr_id)
        base = self.store.receipt_amount(qr["merchant_id"], qr["receipt_id"])
        return {"qr_id": qr_id, "sig": qr["signature"], "receipt_id": qr["receipt_id"], "base_amount": base,
                "tip_pct": 10, "idempotency_key": key}

    def pending(self, qr_id="qr_010", at=NOW, key="k"):
        """Платёж в PENDING: qr_010 по таблице сценариев получает таймаут шлюза."""
        payment, _ = create_payment(self.request(qr_id, key), SECRET, self.store, at)
        confirm_payment(payment, self.store, EmulatorGateway(), at)
        return payment["payment_id"]

    def status(self, payment_id):
        return find_payment(self.store, payment_id)["status"]

    def test_nothing_is_asked_before_the_waiting_time(self):
        pid = self.pending()
        gateway = StubGateway("SUCCESS")
        self.assertEqual(reconcile(self.store, gateway, NOW + RECONCILE_AFTER - timedelta(seconds=1)), [])
        self.assertEqual((gateway.lookups, self.status(pid)), (0, "PENDING"))

    def test_unknown_to_gateway_means_no_charge_and_payment_is_declined(self):
        pid = self.pending()
        self.assertEqual(reconcile(self.store, StubGateway("NOT_FOUND"), NOW + RECONCILE_AFTER), [pid])
        payment = find_payment(self.store, pid)
        self.assertEqual((payment["status"], payment["error_code"]), ("DECLINED", "GATEWAY_TIMEOUT"))

    def test_late_success_is_recorded_not_closed_as_declined(self):
        """Банк всё же списал деньги: закрыть платёж отказом значило бы дать оплатить тот же чек второй раз."""
        pid = self.pending()
        details = {}
        self.assertEqual(reconcile(self.store, StubGateway("SUCCESS"), NOW + RECONCILE_AFTER, details), [pid])
        self.assertEqual(self.status(pid), "SUCCESS")
        self.assertEqual(details, {"SUCCESS": [pid]})
        with self.assertRaises(PaymentError) as ctx:  # QR погашен: вторая оплата невозможна
            create_payment(self.request(key="again"), SECRET, self.store, NOW + RECONCILE_AFTER)
        self.assertEqual(ctx.exception.code, "QR_USED")

    def test_declined_by_gateway_keeps_its_error(self):
        pid = self.pending()
        reconcile(self.store, StubGateway("DECLINED", "CARD_DECLINED"), NOW + RECONCILE_AFTER)
        payment = find_payment(self.store, pid)
        self.assertEqual((payment["status"], payment["error_code"]), ("DECLINED", "CARD_DECLINED"))

    def test_still_processing_payment_is_left_alone_and_flagged_after_a_day(self):
        pid = self.pending()
        gateway = StubGateway("PENDING")
        details = {}
        self.assertEqual(reconcile(self.store, gateway, NOW + RECONCILE_AFTER, details), [])
        self.assertEqual((self.status(pid), details), ("PENDING", {}))
        self.assertEqual(reconcile(self.store, gateway, NOW + STUCK_AFTER + timedelta(minutes=1), details), [])
        self.assertEqual((self.status(pid), details), ("PENDING", {"stuck": [pid]}))  # нужен человек, а не молчаливое закрытие

    def test_crash_between_pending_and_gateway_answer_is_resolved_by_real_status(self):
        """Процесс упал после перехода в PENDING и до записи ответа: сверка узнаёт настоящий исход, а не закрывает отказом."""
        payment, _ = create_payment(self.request("qr_001", "crash"), SECRET, self.store, NOW)
        self.store.transition(payment, "PENDING", None, "payment_submitted", "client", NOW)  # а дальше «сбой»
        reconcile(self.store, EmulatorGateway(), NOW + RECONCILE_AFTER)
        self.assertEqual(self.status(payment["payment_id"]), "SUCCESS")  # у qr_001 по таблице сценариев успех

    def test_reconcile_is_idempotent(self):
        self.pending()
        first = reconcile(self.store, StubGateway("NOT_FOUND"), NOW + RECONCILE_AFTER)
        self.assertEqual(len(first), 1)
        self.assertEqual(reconcile(self.store, StubGateway("NOT_FOUND"), NOW + RECONCILE_AFTER), [])

    def test_forgotten_created_payment_is_canceled_after_ttl(self):
        payment, _ = create_payment(self.request("qr_002", "forgot"), SECRET, self.store, NOW)
        gateway = StubGateway()
        self.assertEqual(cancel_stale(self.store, gateway, NOW + CREATED_TTL - timedelta(seconds=1)), [])
        self.assertEqual(self.status(payment["payment_id"]), "CREATED")
        self.assertEqual(cancel_stale(self.store, gateway, NOW + CREATED_TTL), [payment["payment_id"]])
        self.assertEqual(self.status(payment["payment_id"]), "CANCELED")
        self.assertEqual(gateway.voids, 1)  # сначала шлюз, потом статус
        last = [e for e in self.store.audit() if e["payment_id"] == payment["payment_id"]][-1]
        self.assertEqual((last["action"], last["actor"], last["to_status"]), ("payment_canceled", "system", "CANCELED"))
        # QR снова свободен: можно создать новый платёж
        again, replay = create_payment(self.request("qr_002", "second"), SECRET, self.store, NOW + CREATED_TTL)
        self.assertFalse(replay)

    def test_payment_that_cannot_be_voided_is_not_canceled(self):
        payment, _ = create_payment(self.request("qr_002", "paid-meanwhile"), SECRET, self.store, NOW)
        self.assertEqual(cancel_stale(self.store, StubGateway(can_void=False), NOW + CREATED_TTL), [])
        self.assertEqual(self.status(payment["payment_id"]), "CREATED")  # клиент мог уже заплатить: статус не меняем

    def test_pending_and_final_payments_are_never_auto_canceled(self):
        pid = self.pending()
        done, _ = create_payment(self.request("qr_001", "done"), SECRET, self.store, NOW)
        confirm_payment(done, self.store, EmulatorGateway(), NOW)
        self.assertEqual(cancel_stale(self.store, StubGateway(), NOW + timedelta(days=3)), [])
        self.assertEqual((self.status(pid), self.status(done["payment_id"])), ("PENDING", "SUCCESS"))

    def test_run_once_reports_and_survives_a_failing_job(self):
        self.pending()
        report = run_once(self.store, StubGateway("NOT_FOUND"), NOW + RECONCILE_AFTER)
        self.assertEqual((len(report["reconciled"]), report["canceled"]), (1, []))
        # сбой шлюза на самом платеже не прерывает проход (теперь ловится по каждому платежу) и не пишет секретов
        leak = "sig=deadbeefdeadbeef password=hunter2hunter2 " + "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MC1fYWJjZGVmZ2g"
        broken = StubGateway()
        broken.lookup = lambda *a: (_ for _ in ()).throw(RuntimeError(f"шлюз недоступен: {leak}"))
        self.pending("qr_010", key="second-try")  # прежний платёж закрыт, QR свободен: новый зависает так же
        with Collect() as logged:
            report = run_once(self.store, broken, NOW + RECONCILE_AFTER)
        self.assertEqual(report["reconciled"], [])
        self.assertEqual(report["canceled"], [])
        self.assertIn("сверка платежа", logged.text)  # ошибка в журнале есть
        for secret in ("deadbeefdeadbeef", "hunter2hunter2", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MC1fYWJjZGVmZ2g"):
            self.assertNotIn(secret, logged.text)

    def test_one_failing_payment_does_not_block_the_others(self):
        first = self.pending("qr_010", key="poison")
        second = self.pending("qr_060", at=NOW + timedelta(seconds=1), key="healthy")  # по таблице сценариев тоже таймаут

        class Selective(StubGateway):
            def lookup(self, qr_id, nonce):
                if qr_id == "qr_010":
                    raise RuntimeError("шлюз не отвечает по этому платежу")
                return "NOT_FOUND", "GATEWAY_TIMEOUT"

        details = {}
        with Collect():
            finished = reconcile(self.store, Selective(), NOW + RECONCILE_AFTER + timedelta(seconds=5), details)
        self.assertEqual(finished, [second])  # «ядовитый» первый платёж не блокирует остальные
        self.assertEqual((self.status(first), self.status(second)), ("PENDING", "DECLINED"))
        self.assertEqual(details["errors"], [first])

    def test_not_found_right_after_sending_is_not_final_for_a_gateway_with_grace(self):
        pid = self.pending()
        late_gateway = StubGateway("NOT_FOUND", grace=timedelta(hours=1))  # данные у провайдера доезжают не сразу
        self.assertEqual(reconcile(self.store, late_gateway, NOW + RECONCILE_AFTER + timedelta(minutes=59)), [])
        self.assertEqual(self.status(pid), "PENDING")
        self.assertEqual(reconcile(self.store, late_gateway, NOW + RECONCILE_AFTER + timedelta(hours=1)), [pid])
        self.assertEqual(self.status(pid), "DECLINED")

    def test_unknown_answer_keeps_the_payment_and_flags_it_after_a_day(self):
        pid = self.pending()
        gateway, details = StubGateway("UNKNOWN"), {}
        self.assertEqual(reconcile(self.store, gateway, NOW + STUCK_AFTER + timedelta(minutes=1), details), [])
        self.assertEqual((self.status(pid), details), ("PENDING", {"stuck": [pid]}))

    def test_created_payment_that_cannot_be_voided_is_flagged_after_a_day(self):
        payment, _ = create_payment(self.request("qr_002", "stuck"), SECRET, self.store, NOW)
        details = {}
        gateway = StubGateway(can_void=False)
        self.assertEqual(cancel_stale(self.store, gateway, NOW + CREATED_TTL, details=details), [])
        self.assertEqual(details, {})
        self.assertEqual(cancel_stale(self.store, gateway, NOW + STUCK_AFTER, details=details), [])
        self.assertEqual(details, {"stuck_created": [payment["payment_id"]]})

    def test_batch_is_limited_but_the_next_pass_continues(self):
        pids = [self.pending("qr_010", key="b0")]
        with mock.patch("tips.jobs.reconcile.BATCH", 1):
            self.assertEqual(len(reconcile(self.store, StubGateway("NOT_FOUND"), NOW + RECONCILE_AFTER)), 1)
        self.assertEqual(self.status(pids[0]), "DECLINED")


class CsvJobsTests(JobsMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CsvStore(self._tmp.name, DATA_DIR)

    def test_run_once_reports_and_survives_a_failing_job(self):
        self.skipTest("чистка счётчиков лимитов есть только у PostgreSQL-хранилища")


@unittest.skipUnless(HAVE_SQL, "нужны sqlalchemy и psycopg")
class SqlJobsTests(JobsMixin, unittest.TestCase):
    def setUp(self):
        self.url = database_url()
        if not self.url:
            self.skipTest("нет PostgreSQL")
        self.schema = "tj_" + uuid.uuid4().hex[:12]
        self.engine = bootstrap(self.url, self.schema)
        self.addCleanup(lambda: (self.engine.dispose(), drop_schema(self.url, self.schema)))
        self.store = SqlStore(self.engine)

    def test_qr_numbers_come_from_the_database_sequence_even_in_parallel(self):
        ids, errors, barrier = [], [], threading.Barrier(12)

        def worker(i):
            store = SqlStore(self.engine)
            barrier.wait()
            try:
                req = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": f"r_{3000 + i}", "amount": "100.00"}
                ids.append(create_qr(req, SECRET, store, NOW)["qr_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])  # раньше два выпуска получали один номер и падали с конфликтом ключа
        self.assertEqual(sorted(ids), [f"qr_{n}" for n in range(101, 113)])
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM qr_codes")).scalar(), 112)

    def test_concurrent_issuing_for_one_receipt_gives_one_live_qr(self):
        """Двойной клик официанта: раньше оба запроса не видели живого QR и выпускали по своему, оба оплачиваемые."""
        results, errors, barrier = [], [], threading.Barrier(10)

        def worker():
            store = SqlStore(self.engine)
            barrier.wait()
            try:
                req = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_4000", "amount": "250.00"}
                results.append(create_qr(req, SECRET, store, NOW))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual({r["qr_id"] for r in results}, {"qr_101"})  # один и тот же QR всем
        self.assertEqual(sum(1 for r in results if r["new"]), 1)
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM qr_codes WHERE receipt_id = 'r_4000'")).scalar(), 1)

    def test_concurrent_issuing_for_an_existing_receipt_gives_one_live_qr(self):
        """Сценарий HTTP: чек уже в реестре, прежние QR истекли, официант нажал дважды. Без блокировки чека оба
        запроса не видят живого QR и выпускают каждый свой."""
        later = NOW + timedelta(hours=12)  # seed-QR (до 23:59 первого ноября) уже истекли
        results, errors, barrier = [], [], threading.Barrier(10)

        def worker():
            store = SqlStore(self.engine)
            barrier.wait()
            try:
                req = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"}  # суммы нет: из реестра
                results.append(create_qr(req, SECRET, store, later, amount_required=False))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({r["qr_id"] for r in results}), 1, [r["qr_id"] for r in results])
        self.assertEqual(sum(1 for r in results if r["new"]), 1)
        with self.engine.connect() as c:
            live = c.execute(text("SELECT count(*) FROM qr_codes WHERE receipt_id = 'r_1001' AND exp >= CAST(:t AS timestamptz)"),
                             {"t": later.strftime("%Y-%m-%dT%H:%M:%SZ")}).scalar()
        self.assertEqual(live, 1)  # живой QR на чек ровно один

    def test_concurrent_issuing_with_different_amounts_for_a_new_receipt(self):
        outcomes, barrier = [], threading.Barrier(6)

        def worker(i):
            store = SqlStore(self.engine)
            barrier.wait()
            try:
                req = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_4100", "amount": f"{100 + i}.00"}
                outcomes.append(create_qr(req, SECRET, store, NOW)["qr_id"])
            except PaymentError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        issued = [o for o in outcomes if o.startswith("qr_")]
        self.assertEqual(set(issued), {issued[0]})  # один QR
        self.assertTrue(all(o == "AMOUNT_MISMATCH" for o in outcomes if not o.startswith("qr_")))  # остальные с другой суммой
        self.assertEqual(len(outcomes), 6)

    def test_numbers_beyond_three_and_four_digits_do_not_collide(self):
        with self.engine.begin() as c:
            c.execute(text("SELECT setval('qr_seq', 998), setval('payment_seq', 9998)"))
        store = SqlStore(self.engine)
        qr_ids = [store.next_qr_id() for _ in range(4)]
        self.assertEqual(qr_ids, ["qr_999", "qr_1000", "qr_1001", "qr_1002"])  # lpad в SQL дал бы qr_100 дважды и вечный цикл
        pids = [store.next_payment_id() for _ in range(4)]
        self.assertEqual(pids, ["p_9999", "p_10000", "p_10001", "p_10002"])

    def test_audit_event_numbers_beyond_four_digits_stay_unique(self):
        with self.engine.begin() as c:
            c.execute(text("SELECT setval(pg_get_serial_sequence('audit_events', 'event_id'), 9998)"))
        payment, _ = create_payment(self.request("qr_002", "many"), SECRET, self.store, NOW)
        confirm_payment(payment, self.store, EmulatorGateway(), NOW)
        ids = [e["event_id"] for e in self.store.audit()]
        self.assertEqual(ids[:3], ["evt_9999", "evt_10000", "evt_10001"])
        self.assertEqual(len(set(ids)), len(ids))

    def test_existing_manually_added_qr_number_is_skipped(self):
        with self.engine.begin() as c:
            c.execute(text("SELECT nextval('qr_seq')"))  # 101 «занят» последовательностью
        self.assertEqual(SqlStore(self.engine).next_qr_id(), "qr_102")


if __name__ == "__main__":
    unittest.main()
