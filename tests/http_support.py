"""Общая основа тестов HTTP API: приложение на отдельной схеме PostgreSQL, вход настоящими токенами,
проверка каждого ответа по api/05_api_schemas.yaml (тело проходит схему, код описан для этой операции).
"""
import os
import sys
import threading
import unittest
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "prototype"))
sys.path.insert(0, HERE)

try:
    import jsonschema  # noqa: F401
    import psycopg  # noqa: F401
    import sqlalchemy  # noqa: F401
    import yaml  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:
    from sqlalchemy import text

    import contract_check
    from support import database_url
    from argon2 import PasswordHasher

    from tips.adapters.gateway.emulator import EmulatorGateway
    from tips.adapters.jwt_keys import Keyring, generate_key_pem
    from tips.adapters.passwords import Passwords
    from tips.adapters.sql.admin import bootstrap, drop_schema
    from tips.adapters.sql.store import SqlStore
    from tips.app import users as user_service
    from tips.http.app import create_app

SECRET = os.environ.get("TIPS_HMAC_SECRET", "")
NOW = datetime(2025, 11, 1, 18, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2025, 11, 2, 0, 0, 0, tzinfo=timezone.utc)  # QR из seed уже истекли
TEST_QR = os.path.join(HERE, "..", "prototype", "test_qr_links.csv")
PASSWORD = "correct horse battery staple"
KID = "k-test"
PATH_TEMPLATES = {  # путь запроса -> шаблон из контракта
    "/api/v1/auth/login": "/api/v1/auth/login", "/api/v1/auth/refresh": "/api/v1/auth/refresh",
    "/api/v1/auth/logout": "/api/v1/auth/logout",
    "/api/v1/qr/": "/api/v1/qr/{qr_id}", "/api/v1/tips": "/api/v1/tips", "/api/v1/qr": "/api/v1/qr",
    "/health": "/health", "/api/v1/reports/tips/daily": "/api/v1/reports/tips/daily",
    "/api/v1/reports/tips": "/api/v1/reports/tips",
}


def template(path):
    if path.startswith("/api/v1/payments/"):
        return "/api/v1/payments/{id}/" + path.rsplit("/", 1)[1] if path.count("/") == 5 else "/api/v1/payments/{id}"
    if path.startswith("/api/v1/merchants/"):
        return "/api/v1/merchants/{merchant_id}/status"
    if path.startswith("/api/v1/qr/"):
        return "/api/v1/qr/{qr_id}"
    return PATH_TEMPLATES[path]


class SlowGateway:
    def __init__(self, delay=0.15):
        self.inner = EmulatorGateway()
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def charge(self, qr_id, nonce):
        import time
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return self.inner.charge(qr_id, nonce)


class HttpTestBase(unittest.TestCase):
    def setUp(self):
        self.url = database_url()
        if not self.url:
            self.skipTest("нет PostgreSQL")
        self.schema = "th_" + uuid.uuid4().hex[:12]
        self.engine = bootstrap(self.url, self.schema)
        self.gateway = SlowGateway(delay=0)
        self.now = NOW
        private, self.public_pem = generate_key_pem()
        self.private_pem = private
        self.keyring = Keyring(KID, private)
        # дешёвые параметры argon2 только для скорости тестов; боевые параметры по умолчанию в Passwords()
        self.passwords = Passwords(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
        self.app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords)
        self._tokens = {}
        self.client = TestClient(self.app)
        self.contract = contract_check.Contract()
        self.addCleanup(self._drop)
        self.store = SqlStore(self.engine)

    def _drop(self):
        self.client.close()
        self.engine.dispose()
        drop_schema(self.url, self.schema)
        self.assertEqual(self.contract.failures, [], "ответы разошлись с контрактом")

    # ---- помощники ----

    def call(self, method, path, schema=None, client=None, **kwargs):
        """Запрос с проверкой по контракту: код описан для операции, тело проходит схему."""
        response = (client or self.client).request(method, path, **kwargs)
        op = template(path.split("?")[0])
        self.contract.checked += 1
        if response.status_code not in self.contract.documented(op, method.lower()):
            self.contract.failures.append(f"{method} {op}: код {response.status_code} не описан")
        if response.headers.get("content-type", "").startswith("application/json"):
            body = response.json()
            if "payment_id" in body:
                name = "Payment"
            elif response.status_code >= 400:
                name = "Error"
            else:
                name = schema
            if name:
                self.contract.check(f"{method} {op} -> {response.status_code}", name, body)
        return response

    def qr(self, qr_id):
        row = self.store.find_qr(qr_id)
        return row["signature"], row["receipt_id"], float(self.store.receipt_amount(row["merchant_id"], row["receipt_id"]))

    def tip_body(self, qr_id="qr_001", key=None, **extra):
        sig, receipt, amount = self.qr(qr_id)
        body = {"qr_id": qr_id, "sig": sig, "receipt_id": receipt, "base_amount": amount, "tip_pct": 5,
                "idempotency_key": key or str(uuid.uuid4())}
        body.update(extra)
        return body

    def tip(self, qr_id="qr_001", client=None, **extra):
        return self.call("POST", "/api/v1/tips", client=client, json=self.tip_body(qr_id, **extra))

    def confirm(self, payment, client=None):
        return self.call("POST", f"/api/v1/payments/{payment['payment_id']}/confirm", client=client,
                         json={"confirm_token": payment["confirm_token"]})

    def cancel(self, payment):
        return self.call("POST", f"/api/v1/payments/{payment['payment_id']}/cancel",
                         json={"confirm_token": payment["confirm_token"]})

    def status(self, payment, token=None):
        return self.call("GET", f"/api/v1/payments/{payment['payment_id']}",
                         headers={"X-Payment-Token": token or payment["confirm_token"]})

    def count(self, sql, **params):
        with self.engine.connect() as c:
            return c.execute(text(sql), params).scalar()

    def make_user(self, role, merchant=None, employee=None, login=None, password=PASSWORD):
        login = login or f"{role}-{merchant or 'none'}-{employee or 'none'}"
        user_service.create_user(self.store, self.passwords, login, password, role, merchant, employee, self.now)
        return login

    def login(self, login, password=PASSWORD):
        return self.call("POST", "/api/v1/auth/login", "TokenPair", json={"login": login, "password": password})

    def bearer(self, access_token):
        return {"Authorization": f"Bearer {access_token}"}

    def as_user(self, role, merchant=None, employee=None):
        """Заголовок Authorization настоящего пользователя: создаётся при первом обращении, входит через /auth/login."""
        key = (role, merchant, employee)
        if key not in self._tokens:
            login = self.make_user(role, merchant, employee)
            response = self.login(login)
            self.assertEqual(response.status_code, 200, response.text)
            self._tokens[key] = response.json()["access_token"]
        return self.bearer(self._tokens[key])

