"""Метрики /metrics: включаются токеном, не раскрывают данных, показывают зависшие платежи и очереди.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_metrics
"""
import os
import re
import sys
import unittest
import uuid
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, SECRET, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    from fastapi.testclient import TestClient

    from tips.adapters.provider.sandbox import SandboxBank, SandboxProvider
    from tips.domain.qr_v2 import QrV2Keys
    from tips.http.app import create_app
    from tips.http.limits import Limiter

TOKEN = "t" * 32


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны зависимости сервиса и TIPS_HMAC_SECRET")
class MetricsTests(HttpTestBase):
    def build(self, token):
        if token:
            os.environ["TIPS_METRICS_TOKEN"] = token
        try:
            self.bank = SandboxBank(clock=lambda: self.now)
            return create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                              limiter=Limiter(scale=1000), qr_keys=QrV2Keys({"k1": bytes(range(32))}, "k1"),
                              provider=SandboxProvider(self.bank), front_url="https://tips.example/p")
        finally:
            os.environ.pop("TIPS_METRICS_TOKEN", None)

    def setUp(self):
        super().setUp()
        self.client = TestClient(self.build(TOKEN))

    def get(self, token=TOKEN):
        return self.client.get("/metrics", headers={"Authorization": f"Bearer {token}"} if token else {})

    def value(self, text, name):
        return int(re.search(rf"^{re.escape(name)} (\d+)$", text, re.M).group(1))

    def test_route_is_absent_without_a_token_in_the_environment(self):
        self.assertEqual(TestClient(self.build("")).get("/metrics", headers={"Authorization": "Bearer x"}).status_code, 404)

    def test_wrong_or_missing_token_is_indistinguishable_from_no_route(self):
        for token in (None, "", "wrong", TOKEN[:-1], TOKEN + "x"):
            response = self.get(token)
            self.assertEqual(response.status_code, 404, token)
            self.assertEqual(response.json()["error_code"], "NOT_FOUND")
        self.assertEqual(self.client.get("/metrics", headers={"Authorization": TOKEN}).status_code, 404)  # без Bearer

    def test_output_is_prometheus_text_with_all_statuses_and_no_identifiers(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        for status in ("CREATED", "PENDING", "SUCCESS", "DECLINED", "CANCELED", "REFUND_PENDING", "REFUNDED"):
            self.assertIn(f'tips_payments{{status="{status}"}}', response.text)
        for name in ("tips_oldest_pending_seconds", "tips_webhook_backlog", "tips_refunds_stuck"):
            self.assertIn(f"# TYPE {name} gauge", response.text)
        self.assertNotRegex(response.text, r"pay_|qr_|m_0|e_0|tok_|r_1")

    def test_stuck_payment_and_queues_show_up(self):
        who = self.as_user("waiter", "m_001", "e_001")
        issued = self.client.post("/api/v2/qr", json={"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"},
                                  headers=who).json()
        qr_id, sig = issued["qr_id"], issued["pay_url"].split("&s=")[1]
        view = self.client.get(f"/api/v2/qr/{qr_id}?s={sig}").json()
        payment = self.client.post("/api/v2/payments", headers={"Idempotency-Key": str(uuid.uuid4())}, json={
            "qr_id": qr_id, "sig": sig, "base_amount": view["base_amount"], "tip_percent": 5}).json()
        before = self.get().text
        self.assertEqual(self.value(before, "tips_oldest_created_seconds"), 0)
        self.assertEqual(self.value(before, 'tips_payments{status="CREATED"}'), 1)
        token = self.bank.create_token("4000 0000 0000 0119")  # банк списал, ответ потерян: платёж зависает в PENDING
        self.client.post(f"/api/v2/payments/{payment['payment_id']}/confirm", json={"payment_method_token": token},
                         headers={"X-Payment-Token": payment["confirm_token"]})
        self.now += timedelta(minutes=20)
        after = self.get().text
        self.assertEqual(self.value(after, 'tips_payments{status="PENDING"}'), 1)
        self.assertGreaterEqual(self.value(after, "tips_oldest_pending_seconds"), 20 * 60)


if __name__ == "__main__":
    unittest.main()
