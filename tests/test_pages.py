"""Форма чаевых (/p) и песочница банка (/sandbox): отдача страниц, политика безопасности, сквозной сценарий 3-D Secure.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_pages
"""
import os
import sys
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, SECRET, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    from fastapi.testclient import TestClient

    from tips.adapters.provider.sandbox import SandboxBank, SandboxProvider
    from tips.app.provider_flow import _allowed_action
    from tips.domain.qr_v2 import QrV2Keys
    from tips.http import pages
    from tips.http.app import create_app
    from tips.http.limits import Limiter

KEYS = {"k1": bytes(range(32))}
FRONT = "http://localhost:8083/p"
WEB = os.path.join(HERE, "..", "web", "pay")


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны зависимости сервиса и TIPS_HMAC_SECRET")
class PagesTests(HttpTestBase):
    def build(self, production):
        self.bank = SandboxBank(clock=lambda: self.now, public_url="http://localhost:8083/sandbox")
        self.provider = SandboxProvider(self.bank)
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), qr_keys=QrV2Keys(KEYS, "k1"), provider=self.provider,
                         front_url=FRONT, production=production)
        return TestClient(app)

    def setUp(self):
        super().setUp()
        self.client = self.build(production=False)

    # ---- помощники ----

    def new_payment(self, client=None):
        client = client or self.client
        who = self.as_user("waiter", "m_001", "e_001")
        issued = client.post("/api/v2/qr", json={"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001"},
                             headers=who).json()
        qr_id, sig = issued["qr_id"], issued["pay_url"].split("&s=")[1]
        view = client.get(f"/api/v2/qr/{qr_id}?s={sig}").json()
        created = client.post("/api/v2/payments", headers={"Idempotency-Key": str(uuid.uuid4())}, json={
            "qr_id": qr_id, "sig": sig, "base_amount": view["base_amount"], "tip_percent": 5})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def token(self, number):
        response = self.client.post("/sandbox/tokens", json={"number": number, "exp": "12/30", "cvc": "123"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["payment_method_token"]

    def confirm(self, payment, number):
        return self.client.post(f"/api/v2/payments/{payment['payment_id']}/confirm",
                                json={"payment_method_token": self.token(number)},
                                headers={"X-Payment-Token": payment["confirm_token"]})

    def status(self, payment):
        return self.client.get(f"/api/v2/payments/{payment['payment_id']}",
                               headers={"X-Payment-Token": payment["confirm_token"]}).json()["status"]

    # ---- форма ----

    def test_form_is_served_with_its_own_strict_csp(self):
        for path in ("/p", "/p/", "/p/result"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.text, read("index.html").replace("__SANDBOX__", "1"))
            policy = response.headers["content-security-policy"]
            self.assertIn("script-src 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertNotIn("unsafe-inline", policy)
            self.assertNotIn("unsafe-eval", policy)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/p/app.js").text, read("app.js"))
        self.assertTrue(self.client.get("/p/style.css").headers["content-type"].startswith("text/css"))
        self.assertEqual(self.client.get("/p/secret.txt").status_code, 404)
        self.assertEqual(self.client.get("/p/../app.js").status_code, 404)

    def test_form_is_available_in_production_and_sandbox_is_not(self):
        prod = self.build(production=True)
        self.assertEqual(prod.get("/p").status_code, 200)
        self.assertIn('data-sandbox="0"', prod.get("/p").text)  # в production форма не показывает поля карты и не шлёт их нам
        for path in ("/sandbox", "/sandbox/3ds/sb_" + "0" * 20):
            self.assertEqual(prod.get(path).status_code, 404, path)
        card = {"number": "4242424242424242", "exp": "12/30", "cvc": "123"}
        self.assertEqual(prod.post("/sandbox/tokens", json=card).status_code, 404)
        self.assertEqual(prod.post("/sandbox/settle").status_code, 404)

    def test_form_source_has_no_inline_code_and_no_risky_apis(self):
        html, js = read("index.html"), read("app.js")
        self.assertNotIn("<script>", html)
        self.assertNotIn(" onclick=", html)
        self.assertNotIn("style=", html)
        for risky in ("innerHTML", "outerHTML", "document.write", "eval(", "localStorage", "insertAdjacentHTML"):
            self.assertNotIn(risky, js, risky)
        self.assertIn("textContent", js)
        self.assertEqual(js.count("sessionStorage.setItem"), 1)  # хранится одна запись: платёж и токен подтверждения

    def test_extra_provider_hosts_in_csp_are_https_only(self):
        policy = pages.csp("https://pay.bank.example http://evil.example 'unsafe-inline' "
                           "https://x.example:8443 javascript:alert(1)")
        self.assertIn("https://pay.bank.example", policy)
        self.assertIn("https://x.example:8443", policy)
        for bad in ("evil.example", "unsafe-inline", "javascript"):
            self.assertNotIn(bad, policy)

    # ---- песочница ----

    def test_tokens_endpoint_validates_the_body(self):
        good = {"number": "4242424242424242", "exp": "12/30", "cvc": "123"}
        for body in ({}, {**good, "extra": "x"}, {**good, "number": "42"}, {**good, "number": "4242 4242 4242 4242"},
                     {**good, "exp": "13/30"}, {**good, "cvc": "12"}, {**good, "cvc": 123}):
            self.assertEqual(self.client.post("/sandbox/tokens", json=body).status_code, 400, body)
        response = self.client.post("/sandbox/tokens", json=good)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("4242", response.text)  # номер в ответе не возвращается
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_success_card_pays_without_challenge(self):
        payment = self.new_payment()
        response = self.confirm(payment, "4242424242424242")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "SUCCESS"))

    def test_three_d_secure_page_completes_the_payment_and_returns_to_the_form(self):
        payment = self.new_payment()
        response = self.confirm(payment, "4000000000003220")
        self.assertEqual(response.status_code, 202)
        url = response.json()["next_action"]["url"]
        self.assertTrue(url.startswith("http://localhost:8083/sandbox/3ds/sb_"), url)  # адрес принят: локальная песочница
        path = url[len("http://localhost:8083"):]
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn("default-src 'none'", page.headers["content-security-policy"])
        self.assertNotIn("<script", page.text)
        self.assertEqual(self.status(payment), "PENDING")
        done = self.client.post(path, content=b"result=ok", headers={"Content-Type": "application/x-www-form-urlencoded"},
                                follow_redirects=False)
        self.assertEqual((done.status_code, done.headers["location"]), (303, FRONT + "/result"))
        self.assertEqual(self.status(payment), "SUCCESS")

    def test_three_d_secure_can_be_rejected(self):
        payment = self.new_payment()
        path = self.confirm(payment, "4000000000003220").json()["next_action"]["url"][len("http://localhost:8083"):]
        self.client.post(path, content=b"result=fail", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(self.status(payment), "DECLINED")

    def test_cross_site_posts_to_the_sandbox_are_refused(self):
        payment = self.new_payment()
        path = self.confirm(payment, "4000000000003220").json()["next_action"]["url"][len("http://localhost:8083"):]
        form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example"}
        self.assertEqual(self.client.post(path, content=b"result=ok", headers=form).status_code, 400)
        self.assertEqual(self.client.post("/sandbox/settle", headers={"Origin": "https://evil.example"}).status_code, 400)
        self.assertEqual(self.status(payment), "PENDING")
        own = {**form, "Origin": "http://testserver"}  # свой источник проходит
        self.assertEqual(self.client.post(path, content=b"result=ok", headers=own, follow_redirects=False).status_code, 303)

    def test_three_d_secure_input_is_validated(self):
        payment = self.new_payment()
        path = self.confirm(payment, "4000000000003220").json()["next_action"]["url"][len("http://localhost:8083"):]
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.client.post(path, content=b"result=maybe", headers=form).status_code, 400)
        self.assertEqual(self.client.post(path, content=b"", headers=form).status_code, 400)
        self.assertEqual(self.client.post(path, content=b"result=ok" + b"&x=1" * 600, headers=form).status_code, 400)
        self.assertEqual(self.client.get("/sandbox/3ds/sb_" + "f" * 20).status_code, 404)
        self.assertEqual(self.client.get("/sandbox/3ds/..%2Fp").status_code, 404)
        self.assertEqual(self.client.post("/sandbox/3ds/sb_" + "f" * 20, content=b"result=ok", headers=form).status_code, 404)
        self.assertEqual(self.status(payment), "PENDING")  # ничего из этого платёж не сдвинуло

    def test_lost_response_is_settled_from_the_sandbox_index(self):
        payment = self.new_payment()
        self.assertEqual(self.confirm(payment, "4000000000000119").status_code, 202)
        self.assertEqual(self.status(payment), "PENDING")
        index = self.client.get("/sandbox")
        self.assertEqual(index.status_code, 200)
        for card in ("4242 4242 4242 4242", "4000 0000 0000 3220"):
            self.assertIn(card, index.text)
        self.assertEqual(self.client.post("/sandbox/settle", follow_redirects=False).status_code, 303)
        self.assertEqual(self.status(payment), "SUCCESS")

    def test_http_action_url_is_accepted_only_for_a_local_sandbox(self):
        self.assertTrue(_allowed_action(self.provider, "http://localhost:8083/sandbox/3ds/sb_x"))
        self.assertFalse(_allowed_action(self.provider, "http://evil.example/3ds/x"))  # чужой узел
        self.assertFalse(_allowed_action(self.provider, "javascript:alert(1)"))
        remote = SandboxProvider(SandboxBank(public_url="http://sandbox.bank.example"))
        self.assertFalse(_allowed_action(remote, "http://sandbox.bank.example/3ds/x"))  # без TLS и не локальный
        secure = SandboxProvider(SandboxBank(public_url="https://sandbox.bank.example"))
        self.assertTrue(_allowed_action(secure, "https://sandbox.bank.example/3ds/x"))


if __name__ == "__main__":
    unittest.main()
