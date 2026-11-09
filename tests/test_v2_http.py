"""API v2 через HTTP: QR версии 2, непрозрачные идентификаторы, деньги строками, платёж через песочного провайдера
(токен карты, 3-D Secure, вебхуки), возврат при двойной оплате, отсутствие понижения версии.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_v2_http
"""
import os
import sys
import unittest
import uuid
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, NOW, SECRET, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    from fastapi.testclient import TestClient

    from tips.adapters.provider.sandbox import SandboxBank, SandboxProvider, sign_webhook
    from tips.app.provider_flow import process_refunds
    from tips.domain import ids
    from tips.domain.qr_v2 import QrV2Keys
    from tips.http.app import create_app
    from tips.http.limits import Limiter

KEYS = {"k1": bytes(range(32)), "k2": bytes(range(32, 64))}
CARD = {"success": "4242 4242 4242 4242", "decline": "4000 0000 0000 0002", "challenge": "4000 0000 0000 3220",
        "timeout": "4000 0000 0000 0119"}


def contract_paths(path):
    """Шаблон пути v2 для сообщений (в контракт v1 эти пути не входят)."""
    return path


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны fastapi, httpx, sqlalchemy, psycopg, PyJWT, argon2 и TIPS_HMAC_SECRET")
class V2Tests(HttpTestBase):
    def setUp(self):
        super().setUp()
        self.keys = QrV2Keys(KEYS, "k1")
        self.bank = SandboxBank(clock=lambda: self.now)
        self.provider = SandboxProvider(self.bank)
        self.app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                              limiter=Limiter(scale=1000), qr_keys=self.keys, provider=self.provider,
                              front_url="https://tips.example/p")
        self.client = TestClient(self.app)

    # ---- помощники ----

    def issue(self, receipt="r_1001", employee="e_001", merchant="m_001", who=None):
        who = who or self.as_user("waiter", merchant, employee)
        return self.client.post("/api/v2/qr", json={"merchant_id": merchant, "employee_id": employee, "receipt_id": receipt},
                                headers=who)

    def new_qr(self, receipt="r_1001"):
        response = self.issue(receipt)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        sig = body["pay_url"].split("&s=")[1]
        return body["qr_id"], sig

    def view(self, qr_id, sig):
        return self.client.get(f"/api/v2/qr/{qr_id}?s={sig}")

    def pay_body(self, qr_id, sig, **extra):
        amount = self.view(qr_id, sig).json().get("base_amount", "637.00")  # сумма чека из формы, как делает клиент
        body = {"qr_id": qr_id, "sig": sig, "base_amount": amount, "tip_percent": 5}
        body.update(extra)
        return body

    def create(self, qr_id, sig, key=None, **extra):
        headers = {"Idempotency-Key": key or str(uuid.uuid4())}
        return self.client.post("/api/v2/payments", json=self.pay_body(qr_id, sig, **extra), headers=headers)

    def confirm(self, payment, card="success"):
        token = self.bank.create_token(CARD[card])
        return self.client.post(f"/api/v2/payments/{payment['payment_id']}/confirm",
                                json={"payment_method_token": token}, headers={"X-Payment-Token": payment["confirm_token"]})

    def status(self, payment):
        return self.client.get(f"/api/v2/payments/{payment['payment_id']}",
                               headers={"X-Payment-Token": payment["confirm_token"]})

    def webhooks(self, **kw):
        return [self.client.post("/api/v2/webhooks/sandbox", content=body, headers=h).status_code
                for h, body in self.bank.drain_webhooks(**kw)]

    # ---- QR v2 ----

    def test_issue_qr_v2_has_opaque_id_fragment_link_and_short_ttl(self):
        response = self.issue()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertRegex(body["qr_id"], ids.QR_V2.pattern)
        self.assertRegex(body["pay_url"], r"^https://tips\.example/p#q=qr_[0-9a-hjkmnp-tv-z]{26}&s=[A-Za-z0-9_-]{22}$")
        self.assertEqual(body["exp"], (NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        row = self.store.find_qr(body["qr_id"])
        self.assertEqual((row["version"], row["key_id"], len(row["nonce"])), (2, "k1", 22))

    def test_issue_is_idempotent_per_receipt_and_separate_from_v1(self):
        first, second = self.issue().json(), self.issue().json()
        self.assertEqual(first["qr_id"], second["qr_id"])  # на чек один живой QR версии 2
        v1 = self.client.post("/api/v1/qr", json={"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"},
                              headers=self.as_user("waiter", "m_001", "e_001"))
        self.assertEqual(v1.json()["qr_id"], "qr_001")  # v1 по-прежнему отдаёт свой QR, а не версию 2

    def test_issue_rules_match_v1_and_validate_v2_formats(self):
        self.assertEqual(self.issue(who=self.as_user("waiter", "m_001", "e_002")).status_code, 403)  # официант на другого
        self.assertEqual(self.client.post("/api/v2/qr", json={}).status_code, 401)
        waiter = self.as_user("waiter", "m_001", "e_001")
        for bad in ({"merchant_id": "m_001", "employee_id": "e_001"}, {"merchant_id": "x", "employee_id": "e_001", "receipt_id": "r"},
                    {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "bad id!"},
                    {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001", "amount": "1.00"}):
            self.assertEqual(self.client.post("/api/v2/qr", json=bad, headers=waiter).status_code, 400, bad)

    def test_view_hides_internal_ids_and_returns_money_as_strings(self):
        qr_id, sig = self.new_qr()
        response = self.view(qr_id, sig)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "merchant_name": "Вкус-Сервис Тверская", "employee_name": "Иванов И.", "receipt_id": "r_1001",
            "base_amount": "637.00", "currency": "RUB", "tip_options": [0, 5, 10, 15, 20], "max_tip_percent": 50,
            "exp": (NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")})  # ни merchant_id, ни employee_id

    def test_view_errors_do_not_reveal_whether_the_qr_exists(self):
        qr_id, sig = self.new_qr()
        answers = [self.view(qr_id, "A" * 22), self.view(ids.new_id("qr"), sig), self.view("qr_001", sig), self.view(qr_id, "x"),
                   self.view("zzz", sig)]
        for response in answers:
            self.assertEqual((response.status_code, response.json()["error_code"]), (422, "INVALID_QR"))
        self.assertEqual(len({str(r.json()) for r in answers}), 1)

    def test_expired_qr_and_blocked_merchant(self):
        qr_id, sig = self.new_qr()
        self.now += timedelta(hours=4, seconds=1)
        self.assertEqual(self.view(qr_id, sig).status_code, 410)
        self.now = NOW
        admin = self.as_user("admin")
        self.client.patch("/api/v1/merchants/m_001/status", json={"status": "BLOCKED", "reason": "проверка"}, headers=admin)
        self.assertEqual(self.view(qr_id, sig).status_code, 403)

    # ---- нет понижения версии ----

    def test_v1_and_v2_never_accept_each_others_qr(self):
        qr_id, sig = self.new_qr()
        v1_get = self.client.get(f"/api/v1/qr/{qr_id}?sig={'a' * 16}")
        self.assertEqual(v1_get.status_code, 422)
        v1_pay = self.client.post("/api/v1/tips", json={"qr_id": qr_id, "sig": "a" * 16, "receipt_id": "r_1001",
                                                        "base_amount": 637.0, "tip_pct": 5, "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(v1_pay.status_code, 400)  # формат v1 не принимает идентификатор v2
        v2_old = self.view("qr_001", "A" * 22)
        self.assertEqual(v2_old.status_code, 422)
        seed_sig = self.store.find_qr("qr_001")["signature"]
        v2_pay = self.create("qr_001", seed_sig)
        self.assertEqual(v2_pay.status_code, 400)  # QR v1 по пути v2 не оплатить
        # даже если подменить версию в базе, 16 hex подписи v1 не проходят проверку v2
        with self.engine.begin() as c:
            c.execute(__import__("sqlalchemy").text("UPDATE qr_codes SET version = 2, key_id = 'k1' WHERE qr_id = 'qr_001'"))
        self.assertEqual(self.view("qr_001", seed_sig).status_code, 422)

    # ---- платёж ----

    def test_create_payment_returns_opaque_ids_and_string_money(self):
        qr_id, sig = self.new_qr()
        response = self.create(qr_id, sig)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertRegex(body["payment_id"], ids.PAYMENT_V2.pattern)
        self.assertEqual((body["base_amount"], body["tip_amount"], body["amount_total"], body["currency"], body["status"]),
                         ("637.00", "31.85", "668.85", "RUB", "CREATED"))
        self.assertRegex(body["confirm_token"], r"^[a-f0-9]{32}$")
        row = self.store.get_payment(body["payment_id"])
        self.assertEqual(row["api_version"], 2)

    def test_money_must_be_strings_and_input_is_strict(self):
        qr_id, sig = self.new_qr()
        cases = {
            "число вместо строки": self.pay_body(qr_id, sig, base_amount=637.0),
            "целое число": self.pay_body(qr_id, sig, base_amount=637),
            "три знака": self.pay_body(qr_id, sig, base_amount="637.005"),
            "без знаков": self.pay_body(qr_id, sig, base_amount="637"),
            "оба поля чаевых": self.pay_body(qr_id, sig, tip_amount="1.00"),
            "процент дробный": self.pay_body(qr_id, sig, tip_percent=5.5),
            "процент булев": self.pay_body(qr_id, sig, tip_percent=True),
            "лишнее поле": self.pay_body(qr_id, sig, tip_pct=5),
            "тип чаевых без поля": {k: v for k, v in self.pay_body(qr_id, sig).items() if k != "tip_percent"},
            "нет суммы": {k: v for k, v in self.pay_body(qr_id, sig).items() if k != "base_amount"},
            "плохой чек": self.pay_body(qr_id, sig, receipt_id="bad id"),
        }
        for name, body in cases.items():
            response = self.client.post("/api/v2/payments", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), name)
        self.assertEqual(self.count("SELECT count(*) FROM payments"), 0)

    def test_idempotency_key_must_be_a_canonical_uuid_v4_in_a_header(self):
        qr_id, sig = self.new_qr()
        good = str(uuid.uuid4())
        for bad in (None, "", good.upper(), "{" + good + "}", "urn:uuid:" + good, str(uuid.uuid1()), "x"):
            headers = {} if bad is None else {"Idempotency-Key": bad}
            response = self.client.post("/api/v2/payments", json=self.pay_body(qr_id, sig), headers=headers)
            self.assertEqual(response.status_code, 400, repr(bad))
        body = self.pay_body(qr_id, sig, idempotency_key=good)  # в теле ключ не принимается
        self.assertEqual(self.client.post("/api/v2/payments", json=body, headers={"Idempotency-Key": good}).status_code, 400)

    def test_replay_returns_same_payment_with_a_new_token_and_conflict_on_other_body(self):
        qr_id, sig = self.new_qr()
        key = str(uuid.uuid4())
        first, second = self.create(qr_id, sig, key), self.create(qr_id, sig, key)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["payment_id"], second.json()["payment_id"])
        self.assertNotEqual(first.json()["confirm_token"], second.json()["confirm_token"])
        self.assertEqual(self.confirm(first.json()).status_code, 404)  # старый токен не действует
        other = self.create(qr_id, sig, key, tip_percent=10)
        self.assertEqual((other.status_code, other.json()["error_code"]), (409, "IDEMPOTENCY_CONFLICT"))

    def test_wrong_signature_is_422_and_counts_against_the_pair(self):
        qr_id, sig = self.new_qr()
        body = dict(self.pay_body(qr_id, sig), sig="A" * 22)  # форма открыта с верной подписью, платёж уходит с неверной
        self.client.close()
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(), qr_keys=self.keys, provider=self.provider)
        with TestClient(app) as client:
            codes = [client.post("/api/v2/payments", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}).status_code
                     for _ in range(6)]
        self.assertEqual(codes, [422] * 5 + [429])

    # ---- провайдер ----

    def test_success_flow_and_status_with_header_token(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        confirmed = self.confirm(payment)
        self.assertEqual((confirmed.status_code, confirmed.json()["status"]), (200, "SUCCESS"))
        self.assertNotIn("confirm_token", confirmed.json())
        self.assertEqual(self.status(payment).json()["status"], "SUCCESS")
        self.assertEqual(self.client.get(f"/api/v2/payments/{payment['payment_id']}?confirm_token={payment['confirm_token']}").status_code,
                         400)  # токен в адресе не принимается
        self.assertEqual(self.view(qr_id, sig).status_code, 409)  # QR оплачен

    def test_decline_is_402_with_payment_body(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        response = self.confirm(payment, "decline")
        self.assertEqual((response.status_code, response.json()["status"], response.json()["error_code"]),
                         (402, "DECLINED", "CARD_DECLINED"))

    def test_three_d_secure_returns_202_with_redirect_then_polling_completes(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        pending = self.confirm(payment, "challenge")
        self.assertEqual((pending.status_code, pending.json()["status"]), (202, "PENDING"))
        action = pending.json()["next_action"]
        self.assertEqual(action["type"], "redirect")
        self.assertTrue(action["url"].startswith("https://sandbox.bank.example/3ds/"))
        self.assertEqual(self.status(payment).json()["status"], "PENDING")  # клиент ещё не прошёл проверку
        self.bank.complete_challenge(next(iter(self.bank.payments)), ok=True)
        done = self.status(payment).json()  # опрос сам узнаёт итог у провайдера, вебхук не обязателен
        self.assertEqual((done["status"], done["next_action"]), ("SUCCESS", None))

    def test_lost_provider_response_stays_pending_and_webhook_finishes_it(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        pending = self.confirm(payment, "timeout")
        self.assertEqual((pending.status_code, pending.json()["status"]), (202, "PENDING"))
        self.bank.settle(next(iter(self.bank.payments)), ok=True)
        self.assertEqual(self.webhooks(), [200])
        self.assertEqual(self.status(payment).json()["status"], "SUCCESS")

    def test_repeat_confirm_and_cancel_rules(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        canceled = self.client.post(f"/api/v2/payments/{payment['payment_id']}/cancel", headers={"X-Payment-Token": payment["confirm_token"]})
        self.assertEqual((canceled.status_code, canceled.json()["status"]), (200, "CANCELED"))
        conflict = self.confirm(payment)
        self.assertEqual((conflict.status_code, conflict.json()["error_code"]), (409, "PAYMENT_STATE_CONFLICT"))
        self.assertEqual(self.bank.charge_calls, 0)
        qr2, sig2 = self.new_qr("r_1002")
        second = self.create(qr2, sig2).json()
        self.confirm(second)
        self.confirm(second)  # повтор не вызывает провайдера
        self.assertEqual(self.bank.charge_calls, 1)
        refused = self.client.post(f"/api/v2/payments/{second['payment_id']}/cancel", headers={"X-Payment-Token": second["confirm_token"]})
        self.assertEqual((refused.status_code, refused.json()["error_code"]), (409, "CANCEL_NOT_ALLOWED"))

    def test_confirm_body_and_token_validation(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        pid, token = payment["payment_id"], {"X-Payment-Token": payment["confirm_token"]}
        for bad in ({}, {"payment_method_token": 5}, {"payment_method_token": "short"}, {"payment_method_token": "a" * 200},
                    {"payment_method_token": "tok_abcdefgh1234", "extra": 1}):
            self.assertEqual(self.client.post(f"/api/v2/payments/{pid}/confirm", json=bad, headers=token).status_code, 400, bad)
        good = {"payment_method_token": self.bank.create_token(CARD["success"])}
        other_qr, other_sig = self.new_qr("r_1002")
        foreign = self.create(other_qr, other_sig).json()["confirm_token"]  # токен другого платежа
        for headers in ({}, {"X-Payment-Token": "0" * 32}, {"X-Payment-Token": foreign}):
            response = self.client.post(f"/api/v2/payments/{pid}/confirm", json=good, headers=headers)
            self.assertIn(response.status_code, (400, 404))
        self.assertEqual(self.client.get("/api/v2/payments/pay_" + "a" * 26, headers={"X-Payment-Token": "0" * 32}).status_code, 404)
        self.assertEqual(self.client.get("/api/v2/payments/p_0101", headers={"X-Payment-Token": "0" * 32}).status_code, 400)

    def test_v1_payment_is_invisible_to_v2_and_the_other_way_round(self):
        v1 = self.client.post("/api/v1/tips", json={"qr_id": "qr_001", "sig": self.store.find_qr("qr_001")["signature"],
                                                    "receipt_id": "r_1001", "base_amount": 637.0, "tip_pct": 5,
                                                    "idempotency_key": str(uuid.uuid4())}).json()
        self.assertEqual(self.client.get(f"/api/v2/payments/{v1['payment_id']}", headers={"X-Payment-Token": v1["confirm_token"]}).status_code,
                         400)  # формат идентификатора v1 в v2 не принимается
        qr_id, sig = self.new_qr("r_1002")
        v2 = self.create(qr_id, sig).json()
        self.assertEqual(self.client.get(f"/api/v1/payments/{v2['payment_id']}", headers={"X-Payment-Token": v2["confirm_token"]}).status_code,
                         400)

    # ---- вебхуки ----

    def test_webhook_signature_is_checked_and_duplicates_are_harmless(self):
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        self.confirm(payment, "challenge")
        self.bank.complete_challenge(next(iter(self.bank.payments)), ok=True)
        (headers, body), = self.bank.drain_webhooks()
        stamp = int(self.now.timestamp())
        for name, (h, raw) in {"нет подписи": ({}, body), "тело изменено": (headers, body.replace(b"succeeded", b"failed")),
                               "чужой секрет": (sign_webhook("other", body, stamp), body),
                               "устарела": (sign_webhook(self.bank.webhook_secret, body, stamp - 400), body)}.items():
            response = self.client.post("/api/v2/webhooks/sandbox", content=raw, headers=h)
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), name)
        # неверные вебхуки платёж не меняют (проверяем в базе: опрос статуса сам спросил бы банк, а он уже ответил)
        self.assertEqual(self.store.get_payment(payment["payment_id"])["status"], "PENDING")
        for _ in range(2):
            self.assertEqual(self.client.post("/api/v2/webhooks/sandbox", content=body, headers=headers).status_code, 200)
        self.assertEqual(self.store.get_payment(payment["payment_id"])["status"], "SUCCESS")  # вебхук закрыл платёж сам
        self.assertEqual(self.count("SELECT count(*) FROM gateway_events"), 1)
        self.assertEqual(self.client.post("/api/v2/webhooks/other", content=body, headers=headers).status_code, 404)

    def test_webhooks_are_not_counted_by_the_global_limit(self):
        self.client.close()
        from tips.http.limits import DEFAULT_RULES, Rule
        tight = Limiter(rules=dict(DEFAULT_RULES, **{"global": Rule(2, 60)}))
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords, limiter=tight,
                         qr_keys=self.keys, provider=self.provider)
        body = b'{"id":"evt_a","type":"payment.succeeded","payment_id":"sb_none"}'
        headers = sign_webhook(self.bank.webhook_secret, body, int(self.now.timestamp()))
        with TestClient(app) as client:
            codes = []
            for i in range(10):
                raw = body.replace(b"evt_a", f"evt_{i}".encode())
                codes.append(client.post("/api/v2/webhooks/sandbox", content=raw,
                                         headers=sign_webhook(self.bank.webhook_secret, raw, int(self.now.timestamp()))).status_code)
            self.assertEqual(codes, [200] * 10)
            client.get("/api/v1/nothing"), client.get("/api/v1/nothing")
            self.assertEqual(client.get("/api/v1/nothing").status_code, 429)  # а обычные запросы по-прежнему ограничены
        self.assertTrue(headers)

    def test_double_payment_ends_in_a_refund_through_http(self):
        qr_id, sig = self.new_qr()
        a = self.create(qr_id, sig).json()
        self.confirm(a, "timeout")  # банк списал, ответ потерялся
        self.store.transition(self.store.get_payment(a["payment_id"]), "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", self.now)
        b = self.create(qr_id, sig).json()
        self.assertEqual(self.confirm(b).json()["status"], "SUCCESS")  # клиент заплатил повторно
        self.bank.settle(next(p for p, v in self.bank.payments.items() if v["reference"] == a["payment_id"]), ok=True)
        self.webhooks()
        self.assertEqual(self.status(a).json()["status"], "REFUND_PENDING")
        self.assertEqual(self.count("SELECT count(*) FROM payments WHERE status = 'SUCCESS' AND qr_id = :q", q=qr_id), 1)
        process_refunds(self.store, self.provider, self.now)
        self.assertEqual(self.status(a).json()["status"], "REFUNDED")

    # ---- прочее ----

    def test_v1_guest_routes_can_be_switched_off(self):
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), qr_keys=self.keys, provider=self.provider, enable_v1=False)
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/v1/tips", json={}).status_code, 404)
            self.assertEqual(client.get("/api/v1/qr/qr_001?sig=" + "a" * 16).status_code, 404)
            self.assertEqual(client.post("/api/v1/auth/login", json={"login": "x", "password": "y"}).status_code, 401)  # вход остался
            self.assertEqual(client.get("/health").status_code, 200)

    def test_env_switch_turns_v1_guest_routes_off(self):
        for value, expected in (("off", 404), ("OFF", 404), ("on", 422), ("", 422)):
            os.environ["TIPS_API_V1"] = value
            try:
                app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                                 limiter=Limiter(scale=1000), qr_keys=self.keys, provider=self.provider)
            finally:
                del os.environ["TIPS_API_V1"]
            with TestClient(app) as client:  # пустое тело: при включённом v1 маршрут есть и отвечает ошибкой формата
                code = client.post("/api/v1/tips", json={}, headers={"Idempotency-Key": "k"}).status_code
            self.assertEqual(code == 404, expected == 404, (value, code))

    def test_v2_is_absent_without_keys(self):
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000))
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/v2/qr", json={}).status_code, 404)

    def test_confirm_without_a_provider_is_503_not_a_crash(self):
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), qr_keys=self.keys, production=True)
        qr_id, sig = self.new_qr()
        payment = self.create(qr_id, sig).json()
        with TestClient(app) as client:
            response = client.post(f"/api/v2/payments/{payment['payment_id']}/confirm",
                                   json={"payment_method_token": "tok_abcdefgh1234"},
                                   headers={"X-Payment-Token": payment["confirm_token"]})
            self.assertEqual((response.status_code, response.headers["retry-after"]), (503, "30"))

    def test_key_rotation_old_qr_keeps_working_new_qr_uses_new_key(self):
        old_id, old_sig = self.new_qr()
        rotated = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                             limiter=Limiter(scale=1000), qr_keys=QrV2Keys(KEYS, "k2"), provider=self.provider)
        with TestClient(rotated) as client:
            self.assertEqual(client.get(f"/api/v2/qr/{old_id}?s={old_sig}").status_code, 200)
            fresh = client.post("/api/v2/qr", json={"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1002"},
                                headers=self.as_user("waiter", "m_001", "e_001")).json()
            self.assertEqual(self.store.find_qr(fresh["qr_id"])["key_id"], "k2")
        dropped = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                             limiter=Limiter(scale=1000), qr_keys=QrV2Keys({"k2": KEYS["k2"]}, "k2"), provider=self.provider)
        with TestClient(dropped) as client:
            self.assertEqual(client.get(f"/api/v2/qr/{old_id}?s={old_sig}").status_code, 422)


if __name__ == "__main__":
    unittest.main()
