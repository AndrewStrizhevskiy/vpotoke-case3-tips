"""Защитная обвязка (M5): лимиты запросов, блокировка неверных подписей, заголовки, размер тела, CORS,
журналы без секретов, единый ответ на необработанные ошибки, отключённая документация.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_hardening
"""
import logging
import os
import sys
import unittest
import uuid
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, SECRET, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from tips.http import logs
    from tips.http.app import create_app
    from tips.http.limits import DEFAULT_RULES, Limiter, Rule, client_ip


class Collect(logging.Handler):
    """Собирает журнал так, как его пишет боевой сервис: с очисткой секретов."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []
        self.setFormatter(logs.RedactingFormatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        self.lines.extend(self.format(record).split("\n"))

    @property
    def text(self):
        return "\n".join(self.lines)


class FakeHeaders(dict):
    def getlist(self, name):
        return list(self.get(name, []))


class FakeRequest:
    def __init__(self, host, forwarded=None):
        """forwarded: строка или список строк (прокси может прислать несколько строк заголовка)."""
        self.client = type("C", (), {"host": host})()
        lines = [forwarded] if isinstance(forwarded, str) else list(forwarded or [])
        self.headers = FakeHeaders({"x-forwarded-for": lines} if lines else {})


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны fastapi, httpx, sqlalchemy, psycopg, PyJWT, argon2 и TIPS_HMAC_SECRET")
class HardeningTests(HttpTestBase):
    LIMIT_SCALE = 1  # боевые лимиты

    def get_qr(self, qr_id="qr_001", sig=None, headers=None, client=None):
        sig = sig or self.qr(qr_id)[0]
        return (client or self.client).get(f"/api/v1/qr/{qr_id}?sig={sig}", headers=headers)

    # ---- лимиты ----

    def test_qr_view_limit_and_retry_after_and_window_reset(self):
        statuses = [self.get_qr().status_code for _ in range(31)]
        self.assertEqual(statuses, [200] * 30 + [429])
        blocked = self.get_qr()
        self.assertEqual((blocked.status_code, blocked.json()["error_code"], blocked.json()["retryable"]),
                         (429, "RATE_LIMITED", True))
        expected = 60 - int(self.now.timestamp()) % 60
        self.assertEqual(blocked.headers["retry-after"], str(expected))
        self.now += timedelta(seconds=expected)  # следующее окно
        self.assertEqual(self.get_qr().status_code, 200)

    def test_tip_creation_limit_is_per_qr(self):
        codes = [self.tip(key=str(uuid.uuid4())).status_code for _ in range(11)]  # в этом тесте первый создаёт платёж
        self.assertEqual(codes[0], 201)
        self.assertEqual(codes[-1], 429)  # одиннадцатый запрос в минуту на тот же QR
        self.assertEqual(self.tip("qr_002").status_code, 201)  # другой QR не задет

    def test_payment_actions_are_limited_per_payment(self):
        payment = self.tip().json()
        codes = [self.confirm(payment).status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)
        self.assertNotIn(429, codes[:10])
        other = self.tip("qr_002").json()
        self.assertEqual(self.confirm(other).status_code, 200)

    def test_login_and_refresh_limits(self):
        login = self.make_user("admin", login="boss")
        wrong = {"login": login, "password": "wrong password 12"}
        codes = [self.client.post("/api/v1/auth/login", json=wrong).status_code for _ in range(11)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[-1], 429)
        refresh = [self.client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 40}).status_code for _ in range(21)]
        self.assertEqual((refresh[0], refresh[-1]), (401, 429))

    def test_global_limit_covers_any_path_but_not_health(self):
        for _ in range(60):
            self.assertEqual(self.client.get("/health").status_code, 200)  # проверки живости лимит не тратят
        codes = [self.client.get("/api/v1/nothing").status_code for _ in range(301)]
        self.assertEqual((codes[0], codes[299], codes[300]), (404, 404, 429))
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_protected_operations_are_limited_per_user(self):
        admin = self.as_user("admin")
        path = "/api/v1/reports/tips?merchant_id=m_001&from=2025-11-01&to=2025-11-30"
        codes = [self.client.get(path, headers=admin).status_code for _ in range(121)]
        self.assertEqual((codes[0], codes[119], codes[120]), (200, 200, 429))
        other = self.as_user("merchant", "m_001")
        self.assertEqual(self.client.get(path, headers=other).status_code, 200)  # другой пользователь не задет

    # ---- неверные подписи ----

    def test_five_bad_signatures_block_the_pair_for_the_window(self):
        for _ in range(5):
            self.assertEqual(self.get_qr(sig="0" * 16).status_code, 422)
        blocked = self.get_qr()  # даже верная подпись: пара «адрес и qr_id» заблокирована
        self.assertEqual((blocked.status_code, blocked.json()["error_code"]), (429, "RATE_LIMITED"))
        self.assertEqual(self.tip(sig="0" * 16).status_code, 429)  # и создание платежа тоже
        self.assertEqual(self.get_qr("qr_002").status_code, 200)  # другой QR работает
        wait = int(blocked.headers["retry-after"])
        self.assertLessEqual(wait, 900)
        self.now += timedelta(seconds=wait)
        self.assertEqual(self.get_qr().status_code, 200)

    def test_bad_signatures_on_payment_creation_count_too(self):
        codes = [self.tip(sig="0" * 16).status_code for _ in range(6)]
        self.assertEqual(codes, [422] * 5 + [429])

    # ---- адрес клиента ----

    def test_client_ip_rules(self):
        self.assertEqual(client_ip(FakeRequest("10.0.0.5")), "10.0.0.5")
        # без доверенного прокси заголовок игнорируется
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", "1.2.3.4"), 0), "10.0.0.5")
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", "6.6.6.6, 1.2.3.4"), 1), "1.2.3.4")  # запись нашего прокси, справа
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", "6.6.6.6, 1.2.3.4, 10.9.9.9"), 2), "1.2.3.4")
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", "1.2.3.4"), 2), "10.0.0.5")  # цепочка короче ожидаемой
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", "not-an-ip"), 1), "unknown")
        self.assertEqual(client_ip(FakeRequest("2001:db8:1:2:aaaa::1")), client_ip(FakeRequest("2001:db8:1:2:bbbb::9")))
        self.assertNotEqual(client_ip(FakeRequest("2001:db8:1:2::1")), client_ip(FakeRequest("2001:db8:1:3::1")))

    def test_limits_are_separate_per_client_behind_a_proxy_and_spoofing_does_not_help(self):
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(), proxy_hops=1)
        sig = self.qr("qr_001")[0]
        with TestClient(app) as client:
            def call(real, fake=None):
                xff = f"{fake}, {real}" if fake else real  # прокси дописывает настоящий адрес справа
                return client.get(f"/api/v1/qr/qr_001?sig={sig}", headers={"X-Forwarded-For": xff}).status_code
            self.assertEqual([call("1.1.1.1") for _ in range(31)][-1], 429)
            self.assertEqual(call("1.1.1.1", fake="9.9.9.9"), 429)  # подделанная левая запись не даёт новый лимит
            self.assertEqual(call("2.2.2.2"), 200)  # другой настоящий клиент

    # ---- размер тела ----

    def test_oversized_bodies_are_rejected(self):
        big = '{"login": "' + "a" * 70_000 + '"}'
        response = self.client.post("/api/v1/auth/login", content=big, headers={"content-type": "application/json"})
        self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"))

        def chunks():  # без Content-Length: размер заранее неизвестен
            for _ in range(10):
                yield b"a" * 10_000
        response = self.client.post("/api/v1/auth/login", content=chunks(), headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        small = self.client.post("/api/v1/auth/login", json={"login": "x", "password": "y"})
        self.assertEqual(small.status_code, 401)  # обычные запросы проходят

    # ---- заголовки, документация, CORS ----

    def test_security_headers_on_every_kind_of_response(self):
        responses = [self.client.get("/health"), self.get_qr(), self.get_qr(sig="0" * 16), self.client.get("/api/v1/nothing"),
                     self.client.post("/api/v1/auth/login", json={})]
        for response in responses:
            h = response.headers
            self.assertEqual(h["x-content-type-options"], "nosniff")
            self.assertEqual(h["referrer-policy"], "no-referrer")
            self.assertEqual(h["content-security-policy"], "default-src 'none'; frame-ancestors 'none'")
            self.assertEqual(h["cache-control"], "no-store")
            self.assertIn("includeSubDomains", h["strict-transport-security"])
            self.assertTrue(h["x-request-id"])

    def test_request_id_is_kept_when_sane_and_replaced_otherwise(self):
        ok = self.client.get("/health", headers={"X-Request-ID": "abc-12345678"})
        self.assertEqual(ok.headers["x-request-id"], "abc-12345678")
        for bad in ("short", "has spaces in it 123", "x" * 100, "semi;colon-12345678", "tab\there-12345678"):
            response = self.client.get("/health", headers={"X-Request-ID": bad})
            self.assertNotEqual(response.headers["x-request-id"], bad)
            self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{16}$")

    def test_api_documentation_is_off_in_production_and_on_in_development(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        dev = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), production=False)
        with TestClient(dev) as client:
            self.assertEqual(client.get("/openapi.json").status_code, 200)
            self.assertNotIn("strict-transport-security", client.get("/health").headers)

    def test_cors_is_off_by_default_and_explicit_when_enabled(self):
        self.assertNotIn("access-control-allow-origin",
                         self.client.get("/health", headers={"Origin": "https://evil.example"}).headers)
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), cors_origins=["https://tips.example"])
        with TestClient(app) as client:
            good = client.get("/health", headers={"Origin": "https://tips.example"})
            self.assertEqual(good.headers["access-control-allow-origin"], "https://tips.example")
            self.assertNotIn("access-control-allow-credentials", good.headers)
            evil = client.get("/health", headers={"Origin": "https://evil.example"})
            self.assertNotIn("access-control-allow-origin", evil.headers)
            preflight = client.options("/api/v1/tips", headers={
                "Origin": "https://tips.example", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, x-payment-token"})
            self.assertEqual(preflight.status_code, 200)
        with self.assertRaises(ValueError):
            create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords, cors_origins=["*"])

    # ---- ошибки и журналы ----

    def test_unhandled_error_returns_generic_500_and_logs_without_secrets(self):
        leak = "sig=deadbeefdeadbeef password=hunter2hunter2 token=" + "a" * 40

        def boom(qr_id, nonce):
            raise RuntimeError(f"шлюз упал: {leak}")
        self.gateway.charge = boom
        payment = self.tip().json()
        collected = Collect()
        root = logging.getLogger()
        root.addHandler(collected)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            response = self.confirm(payment)
        finally:
            root.removeHandler(collected)
            root.setLevel(old)
        self.assertEqual((response.status_code, response.json()["error_code"]), (500, "INTERNAL_ERROR"))
        self.assertNotIn("шлюз упал", response.text)  # клиенту подробности не раскрываются
        self.assertNotIn("Traceback", response.text)
        for secret in ("deadbeefdeadbeef", "hunter2hunter2", "a" * 40):
            self.assertNotIn(secret, collected.text)
        self.assertIn("необработанная ошибка", collected.text)  # а сама ошибка в журнале есть
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_access_log_never_contains_secrets(self):
        collected = Collect()
        root = logging.getLogger()
        root.addHandler(collected)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            sig = self.qr("qr_001")[0]
            self.client.get(f"/api/v1/qr/qr_001?sig={sig}")
            payment = self.tip().json()
            self.confirm(payment)
            self.status(payment)
            login = self.make_user("admin", login="boss")
            pair = self.login(login).json()
            self.client.get("/api/v1/reports/tips?merchant_id=m_001&from=2025-11-01&to=2025-11-30",
                            headers=self.bearer(pair["access_token"]))
            self.client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        finally:
            root.removeHandler(collected)
            root.setLevel(old)
        out = collected.text
        secrets_ = (sig, payment["confirm_token"], pair["access_token"], pair["refresh_token"], "correct horse battery staple")
        for secret in secrets_:
            self.assertNotIn(secret, out)
        self.assertRegex(out, r"tips\.access INFO GET /api/v1/qr/qr_001 200 \d+ms ip=")  # путь есть, query нет
        ours = [line for line in collected.lines if line.startswith("tips.")]
        self.assertTrue(ours)
        self.assertFalse([line for line in ours if "?" in line], "в журнале сервиса адрес без query")
        # журнал самого тестового клиента httpx показывает адрес целиком, но очистка маскирует подпись
        self.assertIn("sig=[masked]", out)

    def test_qr_signed_before_key_rotation_still_works_and_stops_after_retirement(self):
        from tips.domain.qr_sign import QrKeys
        sig = self.qr("qr_001")[0]  # seed подписан ключом SECRET
        rotated = create_app(self.engine, self.gateway, QrKeys(b"brand-new-signing-key", [SECRET.encode()]), lambda: self.now,
                             self.keyring, self.passwords, limiter=Limiter(scale=1000))
        dropped = create_app(self.engine, self.gateway, QrKeys(b"brand-new-signing-key"), lambda: self.now,
                             self.keyring, self.passwords, limiter=Limiter(scale=1000))
        with TestClient(rotated) as client:
            self.assertEqual(client.get(f"/api/v1/qr/qr_001?sig={sig}").status_code, 200)
        with TestClient(dropped) as client:
            self.assertEqual(client.get(f"/api/v1/qr/qr_001?sig={sig}").status_code, 422)

    # ---- замечания ревью M5 ----

    def test_non_ascii_signature_is_a_normal_invalid_qr_and_never_a_500(self):
        for sig in ("ééééééééééééééé", "я" * 16):
            response = self.tip(sig=sig)
            self.assertEqual((response.status_code, response.json()["error_code"]), (422, "INVALID_QR"), sig)
        existing = self.client.post("/api/v1/tips", json=dict(self.tip_body(), sig="я" * 16))
        missing = self.client.post("/api/v1/tips", json=dict(self.tip_body(), qr_id="qr_099", sig="я" * 16))
        self.assertEqual((existing.status_code, existing.json()), (missing.status_code, missing.json()))  # не оракул qr_id

    def test_junk_payment_ids_share_one_limit_bucket(self):
        for i in range(40):
            self.client.get(f"/api/v1/payments/junk{i}", headers={"X-Payment-Token": "0" * 32})
        with self.engine.connect() as c:
            buckets = c.execute(
                text("SELECT count(DISTINCT bucket) FROM rate_hits WHERE bucket LIKE 'pay_status:%'")).scalar()
        self.assertEqual(buckets, 1)  # раньше каждый мусорный идентификатор создавал свою строку счётчика

    def test_oversized_keys_cannot_break_the_limiter(self):
        long_id = "p_" + "1" * 5000
        response = self.client.post(f"/api/v1/payments/{long_id}/confirm", json={"confirm_token": "0" * 32})
        self.assertEqual(response.status_code, 400)  # слишком длинный идентификатор: ошибка формата, а не 500 из индекса

    def test_ipv4_mapped_ipv6_is_an_ordinary_ipv4_client(self):
        self.assertEqual(client_ip(FakeRequest("::ffff:1.2.3.4")), "1.2.3.4")
        self.assertNotEqual(client_ip(FakeRequest("::ffff:1.2.3.4")), client_ip(FakeRequest("::ffff:5.6.7.8")))

    def test_every_forwarded_for_header_line_counts(self):
        # прокси дописал отдельную строку вместо продолжения прежней: берётся запись нашего прокси, а не клиента
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", ["6.6.6.6", "1.2.3.4"]), 1), "1.2.3.4")
        self.assertEqual(client_ip(FakeRequest("10.0.0.5", ["6.6.6.6, 7.7.7.7", "1.2.3.4"]), 2), "7.7.7.7")

    def test_non_ascii_content_length_does_not_crash(self):
        """httpx такой заголовок не отправит, поэтому вызываем приложение напрямую, как это сделал бы сырой сокет."""
        import asyncio
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "path": "/health",
                 "raw_path": b"/health", "query_string": b"", "root_path": "", "scheme": "http",
                 "headers": [(b"content-length", "\u00b2".encode("latin-1"))], "client": ("203.0.113.7", 5000),
                 "server": ("testserver", 80)}
        asyncio.run(self.app(scope, receive, send))
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        self.assertLess(status, 500)  # раньше int("²") падал, и клиент получал 500

    def test_control_characters_cannot_forge_log_lines(self):
        """httpx сам вырезает переводы строк из адреса, поэтому проверяем форматтер на записи, как её даст сервер,
        получивший путь с %0A по сырому сокету."""
        formatter = logs.RedactingFormatter("%(name)s %(levelname)s %(message)s")
        record = logging.LogRecord("tips.access", logging.INFO, __file__, 1, "%s %s %s", (
            "GET", "/api/v1/nothing\nERROR tips.fake forged line\r\nanother\x1b[31m", 404), None)
        out = formatter.format(record)
        self.assertEqual(len(out.splitlines()), 1)  # одна запись журнала остаётся одной строкой
        self.assertIn("\\nERROR tips.fake", out)
        self.assertIn("\\r\\n", out)
        self.assertIn("\\x1b", out)

    def test_denied_client_does_not_write_to_the_database_any_more(self):
        for _ in range(40):
            self.get_qr()
        with self.engine.connect() as c:
            before = c.execute(text("SELECT hits FROM rate_hits WHERE bucket LIKE 'qr_view:%'")).scalar()
        for _ in range(25):
            self.assertEqual(self.get_qr().status_code, 429)
        with self.engine.connect() as c:
            after = c.execute(text("SELECT hits FROM rate_hits WHERE bucket LIKE 'qr_view:%'")).scalar()
        self.assertEqual(before, after)  # заблокированный клиент базу не нагружает: отказ из памяти
        self.now += timedelta(seconds=61)
        self.assertEqual(self.get_qr().status_code, 200)  # а в новом окне снова работает

    def test_health_reports_database_state_and_other_routes_fail_generically(self):
        from tips.adapters.sql.engine import make_engine
        dead = create_app(make_engine("postgresql://postgres@127.0.0.1:1/none", "public"), self.gateway, SECRET,
                          lambda: self.now, self.keyring, self.passwords, limiter=Limiter(scale=1000))
        with TestClient(dead, raise_server_exceptions=False) as client:
            health = client.get("/health")
            self.assertEqual((health.status_code, health.json()), (503, {"status": "degraded"}))
            other = client.get("/api/v1/qr/qr_001?sig=" + "0" * 16)
            self.assertEqual((other.status_code, other.json()["error_code"]), (500, "INTERNAL_ERROR"))
            self.assertNotIn("127.0.0.1", other.text)  # адрес базы клиенту не показывается
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_cors_headers_are_present_even_on_guard_responses(self):
        tight = Limiter(rules=dict(DEFAULT_RULES, **{"global": Rule(1, 60)}))
        app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=tight, cors_origins=["https://tips.example"])
        with TestClient(app) as client:
            headers = {"Origin": "https://tips.example"}
            client.get("/api/v1/nothing", headers=headers)
            limited = client.get("/api/v1/nothing", headers=headers)
            self.assertEqual(limited.status_code, 429)
            # фронтенд сможет прочитать Retry-After
            self.assertEqual(limited.headers["access-control-allow-origin"], "https://tips.example")

    def test_swagger_csp_exception_exists_only_in_development(self):
        dev = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords,
                         limiter=Limiter(scale=1000), production=False)
        with TestClient(dev) as client:
            self.assertNotIn("content-security-policy", client.get("/docs").headers)
            self.assertIn("content-security-policy", client.get("/health").headers)

    def test_database_errors_do_not_echo_parameters(self):
        self.assertTrue(self.engine.hide_parameters)

    def test_rate_counters_are_purged_by_age(self):
        self.get_qr()
        with self.engine.connect() as c:
            self.assertGreater(c.execute(text("SELECT count(*) FROM rate_hits")).scalar(), 0)
        self.store.purge_rate_hits(self.now + timedelta(days=2))
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM rate_hits")).scalar(), 0)


class RedactTests(unittest.TestCase):
    def test_masks_secrets_in_common_shapes(self):
        cases = {
            "GET /api/v1/qr/qr_001?sig=68d0f361ac38c12b 200": "68d0f361ac38c12b",
            "тело {'password': 'hunter2hunter2', 'login': 'boss'}": "hunter2hunter2",
            'json {"refresh_token": "abcdefghij1234567890"}': "abcdefghij1234567890",
            "Authorization: Bearer abc.def.ghi-jkl": "abc.def.ghi-jkl",
            "jwt eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1XzEifQ.c2lnbmF0dXJl": "eyJhbGciOiJFUzI1NiJ9",
            "token " + "f" * 40: "f" * 40,
            "карта 4111 1111 1111 1111 оплачена": "4111 1111 1111 1111",
            "nonce=c82561 exp=2025": "c82561",
            "confirm_token=" + "0" * 32: "0" * 32,
        }
        for line, secret in cases.items():
            out = logs.redact(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("[masked", out.replace("****", "[masked") if "карта" in line else out, line)

    def test_base64url_tokens_are_masked_without_a_key_word(self):
        refresh = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MC1fYWJjZGVmZ2g"  # 43 символа token_urlsafe(32)
        self.assertNotIn(refresh, logs.redact(f"запрос с {refresh} отклонён"))
        self.assertNotIn(refresh, logs.redact(f"HTTPError: token={refresh}"))

    def test_formatter_does_not_mutate_the_record_and_survives_bad_arguments(self):
        formatter = logs.RedactingFormatter("%(levelname)s %(message)s")
        try:
            raise RuntimeError("sig=deadbeefdeadbeef")
        except RuntimeError:
            import sys as _sys
            record = logging.LogRecord("t", logging.ERROR, __file__, 1, "ошибка %s", ("password=hunter2hunter2",),
                                       _sys.exc_info())
        out = formatter.format(record)
        self.assertNotIn("deadbeefdeadbeef", out)
        self.assertNotIn("hunter2hunter2", out)
        self.assertIn("Traceback", out)  # трейсбек сохраняется, секреты в нём замаскированы
        self.assertIsNotNone(record.exc_info)  # исходная запись цела для других обработчиков
        self.assertEqual(record.args, ("password=hunter2hunter2",))
        broken = logging.LogRecord("t", logging.ERROR, __file__, 1, "нужно два %s и %s", ("один",), None)
        self.assertIn("нужно два", formatter.format(broken))  # неверные аргументы не роняют вызывающий код

    def test_card_mask_keeps_last_four_and_only_for_valid_numbers(self):
        self.assertIn("****1111", logs.redact("карта 4111 1111 1111 1111"))
        self.assertEqual(logs.redact("метка времени 1730484000123"), "метка времени 1730484000123")  # не номер карты

    def test_ordinary_text_is_untouched(self):
        for line in ("GET /api/v1/payments/p_0101 200 12ms ip=10.0.0.1 rid=abc", "сверка: закрыто 3 платежа p_0101, p_0102"):
            self.assertEqual(logs.redact(line), line)


if __name__ == "__main__":
    unittest.main()
