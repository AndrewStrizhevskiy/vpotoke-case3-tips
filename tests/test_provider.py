"""Платёж через провайдера (песочница): токен карты, 3-D Secure, вебхуки, потерянный ответ, поздний успех, возврат.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_provider
"""
import os
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

try:
    import psycopg  # noqa: F401
    import sqlalchemy  # noqa: F401
    HAVE_SQL = True
except ImportError:
    HAVE_SQL = False

if HAVE_SQL:
    from sqlalchemy import text

    from support import database_url
    from tips.adapters.provider import base as pb
    from tips.adapters.provider.sandbox import SandboxBank, SandboxProvider, sign_webhook
    from tips.adapters.sql.admin import bootstrap, drop_schema
    from tips.adapters.sql.store import SqlStore
    from tips.app.payments import create_payment
    from tips.app.provider_flow import (accept_webhook, confirm_payment_v2, process_inbox, process_refunds,
                                        refresh_payment)
    from tips.domain.errors import PaymentError
    from tips.jobs.reconcile import RECONCILE_AFTER, reconcile
    from tips.jobs.worker import run_once

SECRET = os.environ.get("TIPS_HMAC_SECRET", "").encode()
NOW = datetime(2025, 11, 1, 18, 0, 0, tzinfo=timezone.utc)
RETURN = "https://tips.example/p/result"
CARD = {"success": "4242 4242 4242 4242", "decline": "4000 0000 0000 0002", "challenge": "4000 0000 0000 3220",
        "timeout": "4000 0000 0000 0119"}


@unittest.skipUnless(HAVE_SQL and SECRET, "нужны sqlalchemy, psycopg и TIPS_HMAC_SECRET")
class ProviderFlowTests(unittest.TestCase):
    def setUp(self):
        self.url = database_url()
        if not self.url:
            self.skipTest("нет PostgreSQL")
        self.schema = "tv_" + uuid.uuid4().hex[:12]
        self.engine = bootstrap(self.url, self.schema)
        self.addCleanup(lambda: (self.engine.dispose(), drop_schema(self.url, self.schema)))
        self.store = SqlStore(self.engine)
        self.now = NOW
        self.bank = SandboxBank(clock=lambda: self.now)
        self.provider = SandboxProvider(self.bank)

    # ---- помощники ----

    def payment(self, qr_id="qr_001", key=None):
        qr = self.store.find_qr(qr_id)
        base = self.store.receipt_amount(qr["merchant_id"], qr["receipt_id"])
        req = {"qr_id": qr_id, "sig": qr["signature"], "receipt_id": qr["receipt_id"], "base_amount": base, "tip_pct": 10,
               "idempotency_key": key or str(uuid.uuid4())}
        return create_payment(req, SECRET, self.store, self.now)[0]

    def confirm(self, payment, card="success", at=None):
        token = self.bank.create_token(CARD[card])
        return confirm_payment_v2(payment, self.store, self.provider, token, RETURN, at or self.now)

    def row(self, payment):
        return self.store.get_payment(payment["payment_id"])

    def status(self, payment):
        return self.row(payment)["status"]

    def actions(self, payment):
        return [e["action"] for e in self.store.audit() if e["payment_id"] == payment["payment_id"]]

    def count(self, sql, **kw):
        with self.engine.connect() as c:
            return c.execute(text(sql), kw).scalar()

    def webhooks(self, **kw):
        out = []
        for headers, body in self.bank.drain_webhooks(**kw):
            out.append(accept_webhook(self.store, self.provider, headers, body, self.now))
        return out

    # ---- обычные исходы ----

    def test_success_with_card_token(self):
        p = self.payment()
        result = self.confirm(p)
        self.assertEqual(result["status"], "SUCCESS")
        row = self.row(p)
        self.assertTrue(row["provider_payment_id"].startswith("sb_"))
        self.assertEqual((row["provider"], row["api_version"]), ("sandbox", 1))
        self.assertEqual(self.actions(p), ["payment_created", "payment_submitted", "payment_confirmed"])

    def test_decline_keeps_error_code(self):
        p = self.payment()
        self.assertEqual(self.confirm(p, "decline")["status"], "DECLINED")
        self.assertEqual(self.row(p)["error_code"], "CARD_DECLINED")

    def test_unknown_card_token_is_declined_without_charge(self):
        p = self.payment()
        result = confirm_payment_v2(p, self.store, self.provider, "tok_unknown", RETURN, self.now)
        self.assertEqual((result["status"], result["error_code"]), ("DECLINED", "INVALID_PAYMENT_METHOD"))

    def test_repeat_confirm_does_not_call_the_provider_again(self):
        p = self.payment()
        self.confirm(p)
        self.assertEqual(self.confirm(self.row(p))["status"], "SUCCESS")
        self.assertEqual(self.bank.charge_calls, 1)

    def test_canceled_payment_cannot_be_confirmed(self):
        p = self.payment()
        self.store.transition(p, "CANCELED", None, "payment_canceled", "client", self.now)
        with self.assertRaises(PaymentError) as ctx:
            self.confirm(self.row(p))
        self.assertEqual(ctx.exception.code, "PAYMENT_STATE_CONFLICT")
        self.assertEqual(self.bank.charge_calls, 0)

    # ---- 3-D Secure ----

    def test_three_d_secure_redirect_then_success_by_polling(self):
        p = self.payment()
        pending = self.confirm(p, "challenge")
        self.assertEqual(pending["status"], "PENDING")
        action = self.row(p)["next_action"]
        self.assertEqual(action["type"], "redirect")
        self.assertTrue(action["url"].startswith("https://sandbox.bank.example/3ds/"))
        self.assertEqual(refresh_payment(self.store, self.row(p), self.provider, self.now)["status"], "PENDING")  # ещё не прошёл
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        self.assertEqual(refresh_payment(self.store, self.row(p), self.provider, self.now)["status"], "SUCCESS")
        self.assertIsNone(self.row(p)["next_action"])  # ссылка больше не нужна

    def test_three_d_secure_failure(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=False)
        self.assertEqual(refresh_payment(self.store, self.row(p), self.provider, self.now)["status"], "DECLINED")

    def test_three_d_secure_closed_by_webhook_without_polling(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        self.assertEqual(self.webhooks(), [True])
        self.assertEqual(self.status(p), "SUCCESS")

    def test_foreign_host_in_three_d_secure_url_is_not_shown_to_the_client(self):
        class Evil(SandboxProvider):
            def create_payment(self, *a, **k):
                r = super().create_payment(*a, **k)
                return pb.ProviderResult(r.status, r.provider_payment_id, "https://evil.example/phish", reference=r.reference)

        self.provider = Evil(self.bank)
        p = self.payment()
        self.assertEqual(self.confirm(p, "challenge")["status"], "PENDING")
        self.assertIsNone(self.row(p)["next_action"])  # ссылка не сохранена и клиенту не отдаётся

    def test_abandoned_three_d_secure_is_voided_and_declined_by_reconciliation(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.assertEqual(reconcile(self.store, None, NOW + timedelta(minutes=16), provider=self.provider), [])  # ещё ждём
        self.assertEqual(reconcile(self.store, None, NOW + timedelta(minutes=31), provider=self.provider), [p["payment_id"]])
        row = self.row(p)
        self.assertEqual((row["status"], row["error_code"]), ("DECLINED", "GATEWAY_TIMEOUT"))
        self.assertEqual(self.bank.payments[row["provider_payment_id"]]["state"], "voided")  # у банка платёж отменён

    # ---- потерянный ответ, сверка ----

    def test_lost_response_leaves_pending_and_reconciliation_finds_the_real_outcome(self):
        p = self.payment()
        result = self.confirm(p, "timeout")  # банк списал, ответ не дошёл
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(self.row(p)["provider"], "sandbox")  # провайдер записан до вызова
        self.assertEqual(self.row(p)["provider_payment_id"], "")  # а его номер платежа мы не получили
        later = NOW + RECONCILE_AFTER
        details = {}
        self.assertEqual(reconcile(self.store, None, later, details, self.provider), [])  # банк ещё «в обработке»
        self.bank.settle(next(iter(self.bank.payments)), ok=True)
        self.assertEqual(reconcile(self.store, None, later, details, self.provider), [p["payment_id"]])
        self.assertEqual(self.status(p), "SUCCESS")  # нашли по reference; вслепую отказом не закрыли

    def test_payment_the_provider_never_received_is_declined_after_the_wait(self):
        p = self.payment()
        self.bank.fail_next("create_payment")  # до банка запрос не дошёл
        self.assertEqual(self.confirm(p)["status"], "PENDING")
        early = NOW + RECONCILE_AFTER - timedelta(seconds=1)
        self.assertEqual(reconcile(self.store, None, early, provider=self.provider), [])
        self.assertEqual(reconcile(self.store, None, NOW + RECONCILE_AFTER, provider=self.provider), [p["payment_id"]])
        self.assertEqual((self.status(p), self.row(p)["error_code"]), ("DECLINED", "GATEWAY_TIMEOUT"))

    def test_not_found_grace_for_a_provider_with_delayed_data(self):
        self.provider.not_found_grace = timedelta(hours=1)
        p = self.payment()
        self.bank.fail_next("create_payment")
        self.confirm(p)
        before = NOW + RECONCILE_AFTER + timedelta(minutes=59)
        self.assertEqual(reconcile(self.store, None, before, provider=self.provider), [])
        self.assertEqual(reconcile(self.store, None, NOW + RECONCILE_AFTER + timedelta(hours=1), provider=self.provider),
                         [p["payment_id"]])

    def test_provider_is_idempotent_by_reference(self):
        token = self.bank.create_token(CARD["success"])
        first = self.bank.create_payment("pay_ref", "100.00", "RUB", token, RETURN, "pay_ref")
        again = self.bank.create_payment("pay_ref", "100.00", "RUB", token, RETURN, "pay_ref")
        self.assertEqual(first.provider_payment_id, again.provider_payment_id)
        self.assertEqual(len(self.bank.payments), 1)  # повтор после сбоя не списывает второй раз

    # ---- вебхуки ----

    def test_webhook_duplicates_are_accepted_once(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        self.assertEqual(self.webhooks(duplicate=True), [True, False])  # второй раз событие уже принято
        self.assertEqual(self.status(p), "SUCCESS")
        self.assertEqual(self.count("SELECT count(*) FROM gateway_events"), 1)
        self.assertEqual(self.actions(p).count("payment_confirmed"), 1)

    def test_invalid_webhooks_are_rejected_and_nothing_is_stored(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        headers, body = self.bank.drain_webhooks()[0]
        stamp = int(self.now.timestamp())
        bad = {
            "подделанное тело": (headers, body.replace(b"succeeded", b"failed")),
            "чужой секрет": (sign_webhook("whsec_other", body, stamp), body),
            "нет подписи": ({}, body),
            "устаревшая подпись": (sign_webhook(self.bank.webhook_secret, body, stamp - 301), body),
            "подпись из будущего": (sign_webhook(self.bank.webhook_secret, body, stamp + 301), body),
            "мусор в заголовке": ({"Sandbox-Signature": "t=abc,v1=zz"}, body),
            "не JSON": (sign_webhook(self.bank.webhook_secret, b"{oops", stamp), b"{oops"),
            "нет id": (sign_webhook(self.bank.webhook_secret, b'{"type":"x"}', stamp), b'{"type":"x"}'),
        }
        for name, (hdr, raw) in bad.items():
            with self.assertRaises(pb.WebhookInvalid, msg=name):
                accept_webhook(self.store, self.provider, hdr, raw, self.now)
        self.assertEqual(self.count("SELECT count(*) FROM gateway_events"), 0)
        self.assertEqual(self.status(p), "PENDING")
        self.assertTrue(accept_webhook(self.store, self.provider, headers, body, self.now))  # настоящий проходит

    def test_two_signatures_during_secret_rotation(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        headers, body = self.bank.drain_webhooks()[0]
        stamp = headers["Sandbox-Signature"].split(",")[0]
        mixed = {"Sandbox-Signature": f"{stamp},v1={'0' * 64},{headers['Sandbox-Signature'].split(',')[1]}"}
        self.assertTrue(accept_webhook(self.store, self.provider, mixed, body, self.now))

    def test_event_for_unknown_payment_is_stored_and_harmless(self):
        body = b'{"id":"evt_x1","type":"payment.succeeded","payment_id":"sb_unknown"}'
        headers = sign_webhook(self.bank.webhook_secret, body, int(self.now.timestamp()))
        self.assertTrue(accept_webhook(self.store, self.provider, headers, body, self.now))
        self.assertEqual(self.count("SELECT count(*) FROM gateway_events WHERE processed_at IS NOT NULL"), 1)

    def test_unprocessed_events_are_retried_by_the_worker(self):
        p = self.payment()
        self.confirm(p, "challenge")
        self.bank.complete_challenge(self.row(p)["provider_payment_id"], ok=True)
        self.bank.fail_next("get_status")  # при приёме провайдер недоступен: событие остаётся в ящике
        self.assertEqual(self.webhooks(), [True])
        self.assertEqual(self.status(p), "PENDING")
        self.assertEqual(self.count("SELECT count(*) FROM gateway_events WHERE processed_at IS NULL AND attempts = 1"), 1)
        self.assertEqual(process_inbox(self.store, self.provider, self.now), 1)
        self.assertEqual(self.status(p), "SUCCESS")

    # ---- поздний успех, двойная оплата, возврат ----

    def test_late_success_after_our_decline_is_recorded(self):
        p = self.payment()
        self.confirm(p, "timeout")
        # закрыли вслепую
        self.store.transition(self.row(p), "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", self.now)
        self.bank.settle(next(iter(self.bank.payments)), ok=True)
        self.webhooks()
        self.assertEqual(self.status(p), "SUCCESS")  # деньги списаны: платёж восстановлен, а не потерян
        self.assertIn("payment_late_success", self.actions(p))
        with self.assertRaises(PaymentError) as ctx:  # и QR теперь оплачен
            self.payment()
        self.assertEqual(ctx.exception.code, "QR_USED")

    def double_payment(self):
        """A ушёл в «таймаут» и был закрыт отказом, клиент заплатил B; потом банк подтвердил и A."""
        a = self.payment(key="a-" + uuid.uuid4().hex)
        self.confirm(a, "timeout")
        self.store.transition(self.row(a), "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", self.now)
        b = self.payment(key="b-" + uuid.uuid4().hex)
        self.assertEqual(self.confirm(b, "success")["status"], "SUCCESS")
        pid_a = next(p for p, v in self.bank.payments.items() if v["reference"] == a["payment_id"])
        self.bank.settle(pid_a, ok=True)
        return a, b

    def test_double_payment_becomes_a_refund_not_a_second_success(self):
        a, b = self.double_payment()
        self.webhooks()
        self.assertEqual((self.status(a), self.status(b)), ("REFUND_PENDING", "SUCCESS"))
        self.assertEqual(self.count("SELECT count(*) FROM payments WHERE status = 'SUCCESS' AND qr_id = 'qr_001'"), 1)
        self.assertEqual(self.count("SELECT count(*) FROM refunds WHERE status = 'REQUIRED' AND payment_id = :p",
                                    p=a["payment_id"]), 1)
        refund_id = self.count("SELECT refund_id FROM refunds WHERE payment_id = :p", p=a["payment_id"])
        self.assertEqual(process_refunds(self.store, self.provider, self.now), [refund_id])
        self.assertEqual(self.status(a), "REFUNDED")
        self.assertEqual(self.actions(a)[-2:], ["payment_refund_requested", "payment_refunded"])
        self.webhooks()  # событие о возврате приходит позже и ничего не ломает
        self.assertEqual(self.status(a), "REFUNDED")

    def test_refund_is_requested_once_even_if_the_event_repeats(self):
        a, _ = self.double_payment()
        self.webhooks(duplicate=True)
        self.webhooks()
        self.assertEqual(self.count("SELECT count(*) FROM refunds WHERE payment_id = :p", p=a["payment_id"]), 1)

    def test_refund_retries_then_hands_over_to_a_human(self):
        a, _ = self.double_payment()
        self.webhooks()
        self.bank.fail_next("refund", 99)  # провайдер возвраты не принимает
        details = {}
        for _ in range(7):
            process_refunds(self.store, self.provider, self.now, details)
        self.assertEqual(self.status(a), "REFUND_PENDING")
        self.assertEqual(details["refund_stuck"], [a["payment_id"]])  # после 5 попыток нужен человек
        self.assertEqual(self.count("SELECT attempts FROM refunds WHERE payment_id = :p", p=a["payment_id"]), 5)

    def test_confirm_of_a_payment_whose_qr_is_already_paid_is_refused(self):
        a = self.payment(key="a-" + uuid.uuid4().hex)
        self.confirm(a, "timeout")
        self.store.transition(self.row(a), "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", self.now)
        b = self.payment(key="b-" + uuid.uuid4().hex)  # создан, пока QR свободен
        pid_a = next(p for p, v in self.bank.payments.items() if v["reference"] == a["payment_id"])
        self.bank.settle(pid_a, ok=True)
        self.webhooks()  # A восстановлен как SUCCESS
        calls = self.bank.charge_calls
        with self.assertRaises(PaymentError) as ctx:
            self.confirm(b)
        self.assertEqual(ctx.exception.code, "QR_USED")
        self.assertEqual(self.status(b), "CANCELED")  # отмена сохранилась (не откатилась вместе с ошибкой)
        self.assertEqual(self.bank.charge_calls, calls)  # провайдера не вызывали

    def test_parallel_confirms_charge_once(self):
        p = self.payment()
        token = self.bank.create_token(CARD["success"])
        results, barrier = [], threading.Barrier(8)

        def worker():
            store = SqlStore(self.engine)
            barrier.wait()
            payment = store.get_payment(p["payment_id"])
            results.append(confirm_payment_v2(payment, store, self.provider, token, RETURN, self.now)["status"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.bank.charge_calls, 1)
        self.assertEqual(set(results) - {"PENDING", "SUCCESS"}, set())
        self.assertEqual(self.status(p), "SUCCESS")

    def test_worker_pass_runs_provider_jobs(self):
        a, _ = self.double_payment()
        self.webhooks(duplicate=False)
        report = run_once(self.store, None, self.now + timedelta(minutes=1), self.provider)
        self.assertEqual(len(report["refunds"]), 1)
        self.assertEqual(self.status(a), "REFUNDED")


if __name__ == "__main__":
    unittest.main()
