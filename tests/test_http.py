"""HTTP API: сценарии T3 через клиент FastAPI, токен платежа, форма чаевых, гонки и соответствие контракту.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_http

Каждый ответ проверяется по api/05_api_schemas.yaml: тело проходит схему, а код описан для этой операции.
Нужны fastapi, httpx, sqlalchemy, psycopg, pyyaml, jsonschema и PostgreSQL (см. test_postgres.py).
Права по ролям, JWT и вход проверяются в test_auth.py.
"""
import os
import sys
import threading
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, LATER, SECRET, TEST_QR, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    from fastapi.testclient import TestClient

    from tips.adapters.sql.seed import add_extra_qrs


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны fastapi, httpx, sqlalchemy, psycopg, pyyaml, jsonschema и TIPS_HMAC_SECRET")
class HttpTests(HttpTestBase):
    # ---- сценарии T3 ----

    def test_health(self):
        self.assertEqual(self.call("GET", "/health").json(), {"status": "ok"})

    def test_t3_1_success_flow(self):
        created = self.tip()
        self.assertEqual(created.status_code, 201)
        payment = created.json()
        self.assertEqual((payment["status"], payment["tip_amount"], payment["amount_total"]), ("CREATED", 31.85, 668.85))
        self.assertRegex(payment["confirm_token"], r"^[a-f0-9]{32}$")
        confirmed = self.confirm(payment)
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["status"], "SUCCESS")
        self.assertNotIn("confirm_token", confirmed.json())  # токен показывается один раз
        self.assertEqual(self.status(payment).json()["status"], "SUCCESS")

    def test_t3_2_declined_is_402_with_payment_body(self):
        payment = self.tip("qr_003", tip_pct=15).json()
        response = self.confirm(payment)
        self.assertEqual(response.status_code, 402)
        body = response.json()
        self.assertEqual((body["status"], body["error_code"], body["tip_amount"], body["amount_total"]),
                         ("DECLINED", "CARD_DECLINED", 136.65, 1047.65))

    def test_t3_3_bad_signature_creates_nothing(self):
        response = self.tip(sig="0" * 16)
        self.assertEqual((response.status_code, response.json()["error_code"]), (422, "INVALID_QR"))
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 0)

    def test_t3_3b_expired_qr(self):
        self.now = LATER
        response = self.tip()
        self.assertEqual((response.status_code, response.json()["error_code"], response.json()["retryable"]),
                         (410, "QR_EXPIRED", True))

    def test_t3_3c_foreign_receipt(self):
        response = self.tip("qr_004", receipt_id="r_9999")
        self.assertEqual((response.status_code, response.json()["error_code"]), (422, "RECEIPT_MISMATCH"))

    def test_t3_4_replay_returns_same_payment_and_fresh_token(self):
        key = str(uuid.uuid4())
        first = self.tip(key=key)
        second = self.tip(key=key)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["payment_id"], second.json()["payment_id"])
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 1)
        # токен показывали один раз и в базе только хеш: при повторе выдаётся новый, старый перестаёт работать
        self.assertNotEqual(first.json()["confirm_token"], second.json()["confirm_token"])
        self.assertEqual(self.confirm(first.json()).status_code, 404)
        self.assertEqual(self.confirm(second.json()).status_code, 200)

    def test_t3_4b_replay_after_success_still_returns_payment(self):
        key = str(uuid.uuid4())
        payment = self.tip(key=key).json()
        self.confirm(payment)
        again = self.tip(key=key)
        self.assertEqual((again.status_code, again.json()["payment_id"]), (200, payment["payment_id"]))

    def test_t3_4c_same_key_other_body_conflicts(self):
        key = str(uuid.uuid4())
        self.tip(key=key)
        response = self.tip(key=key, tip_pct=10)
        self.assertEqual((response.status_code, response.json()["error_code"]), (409, "IDEMPOTENCY_CONFLICT"))

    def test_t3_5_cancel_rules(self):
        payment = self.tip().json()
        canceled = self.cancel(payment)
        self.assertEqual((canceled.status_code, canceled.json()["status"]), (200, "CANCELED"))
        self.assertEqual(self.cancel(payment).json()["status"], "CANCELED")  # повторная отмена
        conflict = self.confirm(payment)
        self.assertEqual((conflict.status_code, conflict.json()["error_code"]), (409, "PAYMENT_STATE_CONFLICT"))

    def test_t3_5b_cancel_after_send_is_forbidden_and_timeout_is_202(self):
        payment = self.tip("qr_010").json()
        pending = self.confirm(payment)
        self.assertEqual((pending.status_code, pending.json()["status"], pending.json()["error_code"]),
                         (202, "PENDING", "GATEWAY_TIMEOUT"))
        refused = self.cancel(payment)
        self.assertEqual((refused.status_code, refused.json()["error_code"]), (409, "CANCEL_NOT_ALLOWED"))
        self.assertEqual(self.confirm(payment).status_code, 202)  # повторный confirm: тот же ответ, шлюз не вызывается
        self.assertEqual(self.gateway.calls, 1)

    def test_t3_5c_repeat_confirm_returns_first_answer(self):
        payment = self.tip().json()
        self.assertEqual(self.confirm(payment).status_code, 200)
        self.assertEqual(self.confirm(payment).status_code, 200)
        declined = self.tip("qr_003").json()
        self.assertEqual((self.confirm(declined).status_code, self.confirm(declined).status_code), (402, 402))
        self.assertEqual(self.gateway.calls, 2)

    def test_t3_7_blocked_merchant(self):
        add_extra_qrs(self.engine, TEST_QR)
        body = self.tip_body("qr_001")
        with open(TEST_QR, encoding="utf-8") as f:
            sig = f.read().splitlines()[1].split(",")[6]
        body.update({"qr_id": "qr_101", "sig": sig, "receipt_id": "r_1101", "base_amount": 500})
        response = self.call("POST", "/api/v1/tips", json=body)
        self.assertEqual((response.status_code, response.json()["error_code"]), (403, "MERCHANT_BLOCKED"))

    def test_t3_8_tip_over_50_percent(self):
        response = self.tip(tip_pct=51)
        self.assertEqual((response.status_code, response.json()["error_code"], response.json()["retryable"]),
                         (422, "TIP_OUT_OF_RANGE", True))

    def test_t3_9_second_payment_after_success(self):
        self.confirm(self.tip().json())
        response = self.tip()
        self.assertEqual((response.status_code, response.json()["error_code"]), (409, "QR_USED"))

    def test_second_payment_while_first_is_open(self):
        self.tip()
        response = self.tip()
        self.assertEqual((response.status_code, response.json()["error_code"]), (409, "PAYMENT_IN_PROGRESS"))

    # ---- проверка запроса ----

    def test_validation_errors_are_400(self):
        cases = {
            "ключ не UUID": self.tip_body(key="not-a-uuid"),
            "нет подписи": {k: v for k, v in self.tip_body().items() if k != "sig"},
            "оба поля чаевых": self.tip_body(tip_amount=3),
            "нет суммы": {k: v for k, v in self.tip_body().items() if k != "base_amount"},
            "плохой qr_id": dict(self.tip_body(), qr_id="qr_1"),
        }
        for name, body in cases.items():
            response = self.call("POST", "/api/v1/tips", json=body)
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), name)
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 0)

    def test_malformed_and_non_object_bodies(self):
        for raw in ("{не json", "[1, 2]", "null", ""):
            response = self.call("POST", "/api/v1/tips", content=raw, headers={"content-type": "application/json"})
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), raw)

    def test_amount_with_extra_digits_is_rejected_not_rounded(self):
        sig, receipt, _ = self.qr("qr_001")
        raw = ('{"qr_id": "qr_001", "sig": "%s", "receipt_id": "%s", "base_amount": 637.005, "tip_pct": 5, '
               '"idempotency_key": "%s"}' % (sig, receipt, uuid.uuid4()))
        response = self.call("POST", "/api/v1/tips", content=raw, headers={"content-type": "application/json"})
        self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"))

    def test_wrong_amount_is_422(self):
        response = self.tip(base_amount=1.0)
        self.assertEqual((response.status_code, response.json()["error_code"]), (422, "AMOUNT_MISMATCH"))

    # ---- токен платежа ----

    def test_token_protects_payment(self):
        mine = self.tip("qr_001").json()
        other = self.tip("qr_002").json()
        pid = mine["payment_id"]
        for token in (other["confirm_token"], "0" * 32, "x" * 40):  # чужой, угаданный, не токен
            for method, path, kwargs in (
                    ("POST", f"/api/v1/payments/{pid}/confirm", {"json": {"confirm_token": token}}),
                    ("POST", f"/api/v1/payments/{pid}/cancel", {"json": {"confirm_token": token}}),
                    ("GET", f"/api/v1/payments/{pid}", {"headers": {"X-Payment-Token": token}})):
                response = self.call(method, path, **kwargs)
                self.assertEqual((response.status_code, response.json()["error_code"]), (404, "NOT_FOUND"), (method, token))
        self.assertEqual(self.status(mine).json()["status"], "CREATED")  # платёж не тронут
        self.assertEqual(self.call("GET", "/api/v1/payments/p_9999",
                                   headers={"X-Payment-Token": "0" * 32}).status_code, 404)  # нет такого платежа

    def test_token_format_errors_are_400(self):
        payment = self.tip().json()
        pid = payment["payment_id"]
        self.assertEqual(self.call("GET", f"/api/v1/payments/{pid}").status_code, 400)  # нет заголовка
        self.assertEqual(self.call("GET", f"/api/v1/payments/{pid}?confirm_token={payment['confirm_token']}").status_code,
                         400)  # токен в адресе не принимается
        self.assertEqual(self.call("POST", f"/api/v1/payments/{pid}/confirm", json={"confirm_token": "short"}).status_code, 400)
        self.assertEqual(self.call("POST", f"/api/v1/payments/{pid}/confirm", json={}).status_code, 400)
        self.assertEqual(self.call("GET", "/api/v1/payments/abc", headers={"X-Payment-Token": "0" * 32}).status_code, 400)

    def test_only_token_hash_is_stored(self):
        payment = self.tip().json()
        stored = self.count("SELECT confirm_token_hash FROM payments WHERE payment_id = :p", p=payment["payment_id"])
        self.assertEqual(len(bytes(stored)), 32)
        self.assertNotIn(payment["confirm_token"].encode(), bytes(stored))

    # ---- форма чаевых: GET /qr/{id} ----

    def test_qr_view(self):
        sig, _, _ = self.qr("qr_001")
        response = self.call("GET", f"/api/v1/qr/qr_001?sig={sig}", "QRResponse")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["merchant_name"], body["employee_name"], body["base_amount"], body["tip_options"],
                          body["max_tip_pct"]), ("Вкус-Сервис Тверская", "Иванов И.", 637.0, [0, 5, 10, 15, 20], 50))

    def test_qr_view_errors(self):
        sig, _, _ = self.qr("qr_001")
        bad = self.call("GET", "/api/v1/qr/qr_001?sig=" + "0" * 16)
        unknown = self.call("GET", "/api/v1/qr/qr_999?sig=" + "0" * 16)
        malformed = self.call("GET", "/api/v1/qr/zzz?sig=abc")
        missing = self.call("GET", "/api/v1/qr/qr_001")
        for response in (bad, unknown, malformed, missing):  # не раскрываем, существует ли такой qr_id
            self.assertEqual((response.status_code, response.json()["error_code"]), (422, "INVALID_QR"))
        self.assertEqual(bad.json(), unknown.json())
        self.confirm(self.tip().json())
        used = self.call("GET", f"/api/v1/qr/qr_001?sig={sig}")
        self.assertEqual((used.status_code, used.json()["error_code"]), (409, "QR_USED"))
        self.now = LATER
        expired = self.call("GET", f"/api/v1/qr/qr_002?sig={self.qr('qr_002')[0]}")
        self.assertEqual((expired.status_code, expired.json()["error_code"]), (410, "QR_EXPIRED"))

    # ---- защищённые операции ----

    def test_protected_operations_need_credentials(self):
        calls = [("POST", "/api/v1/qr", {"json": {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"}}),
                 ("GET", "/api/v1/reports/tips?merchant_id=m_001&from=2025-11-01&to=2025-11-30", {}),
                 ("GET", "/api/v1/reports/tips/daily?merchant_id=m_001&from=2025-11-01&to=2025-11-30", {}),
                 ("PATCH", "/api/v1/merchants/m_001/status", {"json": {"status": "BLOCKED", "reason": "тест"}})]
        for headers in ({}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic YWRtaW46YWRtaW4="}):
            for method, path, kwargs in calls:
                response = self.call(method, path, headers=headers, **kwargs)
                self.assertEqual((response.status_code, response.json()["error_code"]), (401, "ACCESS_DENIED"), path)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_issue_qr_rules(self):
        body = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"}
        own = self.call("POST", "/api/v1/qr", "QRCreated", json=body, headers=self.as_user("waiter", "m_001", "e_001"))
        self.assertEqual((own.status_code, own.json()["qr_id"]), (201, "qr_001"))  # на чек уже есть действующий QR
        for who in (self.as_user("waiter", "m_001", "e_002"),   # официант на другого
                    self.as_user("merchant", "m_002"),          # чужое заведение
                    self.as_user("admin")):                     # администратор QR не выпускает
            response = self.call("POST", "/api/v1/qr", json=body, headers=who)
            self.assertEqual((response.status_code, response.json()["error_code"]), (403, "ACCESS_DENIED"))
        merchant = self.as_user("merchant", "m_001")
        self.assertEqual(self.call("POST", "/api/v1/qr", "QRCreated", json=body, headers=merchant).status_code, 201)
        unknown = self.call("POST", "/api/v1/qr", json=dict(body, receipt_id="r_7777"), headers=merchant)
        self.assertEqual((unknown.status_code, unknown.json()["error_code"]), (404, "NOT_FOUND"))  # чека нет в реестре кассы
        with_amount = self.call("POST", "/api/v1/qr", json=dict(body, amount=1), headers=merchant)
        self.assertEqual(with_amount.status_code, 400)  # сумму от клиента не принимаем
        stranger = self.call("POST", "/api/v1/qr", json=dict(body, employee_id="e_004"), headers=merchant)
        self.assertEqual((stranger.status_code, stranger.json()["error_code"]), (403, "ACCESS_DENIED"))  # чужой официант

    def test_reports_and_their_access(self):
        query = "from=2025-11-01&to=2025-11-30"
        merchant = self.as_user("merchant", "m_001")
        report = self.call("GET", f"/api/v1/reports/tips?merchant_id=m_001&{query}", "Report", headers=merchant).json()
        self.assertEqual((report["tips_total"], report["payments_count"], report["avg_tip_pct"], report["success_rate"]),
                         (414.85, 6, 9.0, 0.83))
        waiter = self.call("GET", f"/api/v1/reports/tips?employee_id=e_001&{query}", "EmployeeReport",
                           headers=self.as_user("waiter", "m_001", "e_001")).json()
        self.assertEqual((waiter["tips_total"], waiter["payments_count"], waiter["top_day"]), (83.6, 2, "2025-11-03"))
        daily = self.call("GET", f"/api/v1/reports/tips/daily?merchant_id=m_001&{query}", "DailyReport", headers=merchant).json()
        self.assertEqual(len(daily["daily"]), 2)
        self.assertEqual(self.call("GET", f"/api/v1/reports/tips?merchant_id=m_005&{query}", "Report",
                                   headers=self.as_user("admin")).status_code, 200)
        denied = [(f"/api/v1/reports/tips?merchant_id=m_002&{query}", merchant),            # чужое заведение
                  (f"/api/v1/reports/tips?employee_id=e_002&{query}", self.as_user("waiter", "m_001", "e_001")),
                  (f"/api/v1/reports/tips?merchant_id=m_001&{query}", self.as_user("waiter", "m_001", "e_001")),
                  (f"/api/v1/reports/tips/daily?merchant_id=m_002&{query}", merchant)]
        for path, who in denied:
            response = self.call("GET", path, headers=who)
            self.assertEqual((response.status_code, response.json()["error_code"]), (403, "ACCESS_DENIED"), path)
        for path in (f"/api/v1/reports/tips?{query}",                                      # ни одного идентификатора
                     f"/api/v1/reports/tips?merchant_id=m_001&employee_id=e_001&{query}",  # оба
                     "/api/v1/reports/tips?merchant_id=m_001&from=2025-11-30&to=2025-11-01",
                     f"/api/v1/reports/tips?merchant_id=x&{query}",
                     f"/api/v1/reports/tips/daily?{query}"):
            response = self.call("GET", path, headers=self.as_user("admin"))
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), path)
        missing = self.call("GET", f"/api/v1/reports/tips?merchant_id=m_777&{query}", headers=self.as_user("admin"))
        self.assertEqual(missing.status_code, 404)

    def test_admin_blocks_merchant_and_event_is_logged(self):
        sig, _, _ = self.qr("qr_001")
        self.assertEqual(self.call("GET", f"/api/v1/qr/qr_001?sig={sig}").status_code, 200)
        response = self.call("PATCH", "/api/v1/merchants/m_001/status", json={"status": "BLOCKED", "reason": "проверка"},
                             headers=self.as_user("admin"))
        self.assertEqual((response.status_code, response.json()), (200, {"merchant_id": "m_001", "status": "BLOCKED"}))
        blocked = self.call("GET", f"/api/v1/qr/qr_001?sig={sig}")
        self.assertEqual((blocked.status_code, blocked.json()["error_code"]), (403, "MERCHANT_BLOCKED"))
        self.assertEqual(self.tip().status_code, 403)
        self.assertEqual(self.count("SELECT count(*) FROM merchant_status_events WHERE old_status = 'ACTIVE' "
                                    "AND new_status = 'BLOCKED' AND reason = 'проверка'"), 1)
        self.call("PATCH", "/api/v1/merchants/m_001/status", json={"status": "ACTIVE", "reason": "снято"},
                  headers=self.as_user("admin"))
        self.assertEqual(self.call("GET", f"/api/v1/qr/qr_001?sig={sig}").status_code, 200)

    def test_merchant_status_rules(self):
        body = {"status": "BLOCKED", "reason": "тест"}
        for who in (self.as_user("merchant", "m_001"), self.as_user("waiter", "m_001", "e_001")):
            response = self.call("PATCH", "/api/v1/merchants/m_001/status", json=body, headers=who)
            self.assertEqual((response.status_code, response.json()["error_code"]), (403, "ACCESS_DENIED"))
        admin = self.as_user("admin")
        self.assertEqual(self.call("PATCH", "/api/v1/merchants/m_777/status", json=body, headers=admin).status_code, 404)
        for bad in ({"status": "PENDING", "reason": "x"}, {"status": "BLOCKED"}, {"status": "BLOCKED", "reason": ""},
                    {"status": "BLOCKED", "reason": "x" * 201}):
            self.assertEqual(self.call("PATCH", "/api/v1/merchants/m_001/status", json=bad, headers=admin).status_code, 400)
        self.assertEqual(self.store.merchant_status("m_001"), "ACTIVE")

    def test_unknown_route_uses_error_format(self):
        response = self.client.get("/api/v1/nothing")
        self.assertEqual((response.status_code, response.json()["error_code"]), (404, "NOT_FOUND"))

    # ---- гонки через HTTP ----

    def test_parallel_requests_to_one_qr_create_one_payment(self):
        results = []
        barrier = threading.Barrier(12)

        def worker():
            with TestClient(self.app) as client:
                barrier.wait()
                results.append(self.tip(client=client))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes, [201] + [409] * 11)
        self.assertEqual({r.json()["error_code"] for r in results if r.status_code == 409}, {"PAYMENT_IN_PROGRESS"})
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 1)

    def test_parallel_confirms_call_gateway_once(self):
        payment = self.tip().json()
        self.gateway.delay = 0.2
        results = []
        barrier = threading.Barrier(8)

        def worker():
            with TestClient(self.app) as client:
                barrier.wait()
                results.append(self.confirm(payment, client=client))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.gateway.calls, 1)
        self.assertTrue(all(r.status_code in (200, 202) for r in results), [r.status_code for r in results])
        self.assertEqual(self.status(payment).json()["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
