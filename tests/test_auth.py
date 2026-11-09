"""Вход, JWT и права по ролям (M4).

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_auth

Проверяется то, что ломает защиту на практике: подмена алгоритма (none, HS256 открытым ключом), чужой ключ и kid,
подделанные claims, просроченные и «будущие» токены, отозванные и закрытые пользователи, повторное предъявление
refresh-токена, перебор паролей, а также полная матрица «роль × операция».
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
import uuid
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from http_support import HAVE_DEPS, KID, PASSWORD, SECRET, HttpTestBase  # noqa: E402

if HAVE_DEPS:
    import jwt
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from tips.adapters.jwt_keys import Keyring, generate_key_pem
    from tips.adapters.passwords import Passwords
    from tips.app import auth as auth_service
    from tips.app import users as user_service
    from tips.http.app import create_app


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@unittest.skipUnless(HAVE_DEPS and SECRET, "нужны fastapi, httpx, sqlalchemy, psycopg, PyJWT, argon2 и TIPS_HMAC_SECRET")
class AuthTests(HttpTestBase):
    REPORT = "from=2025-11-01&to=2025-11-30"

    # ---- помощники ----

    def claims_of(self, token):
        return jwt.decode(token, options={"verify_signature": False})

    def signed(self, claims, key=None, kid=KID, alg="ES256"):
        return jwt.encode(claims, key or self.private_pem, algorithm=alg, headers={"kid": kid})

    def good_claims(self, **changes):
        login = self.make_user("admin", login=f"adm-{uuid.uuid4().hex[:8]}")
        claims = self.claims_of(self.login(login).json()["access_token"])
        claims.update(changes)
        return {k: v for k, v in claims.items() if v is not None}

    def status_with(self, token, path=None):
        path = path or f"/api/v1/reports/tips?merchant_id=m_001&{self.REPORT}"
        return self.client.get(path, headers=self.bearer(token)).status_code

    def refresh(self, token):
        return self.call("POST", "/api/v1/auth/refresh", "TokenPair", json={"refresh_token": token})

    def events(self, user_id=None):
        sql = "SELECT action FROM auth_events" + (" WHERE user_id = :u" if user_id else "") + " ORDER BY event_id"
        with self.engine.connect() as c:
            return [r[0] for r in c.execute(text(sql), {"u": user_id} if user_id else {})]

    def user_row(self, login):
        return self.store.user_by_login(login)

    # ---- вход ----

    def test_login_success_returns_pair_with_expected_claims(self):
        login = self.make_user("waiter", "m_001", "e_001")
        response = self.login(login)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["token_type"], body["expires_in"]), ("Bearer", 900))
        self.assertEqual(response.headers["cache-control"], "no-store")
        claims = self.claims_of(body["access_token"])
        self.assertEqual((claims["role"], claims["merchant_id"], claims["employee_id"], claims["iss"], claims["aud"]),
                         ("waiter", "m_001", "e_001", "tips", "tips-api"))
        self.assertEqual(claims["exp"] - claims["iat"], 900)
        self.assertEqual(jwt.get_unverified_header(body["access_token"]), {"alg": "ES256", "kid": KID, "typ": "JWT"})
        self.assertEqual(len(claims["jti"]), 32)
        self.assertEqual(self.events(), ["user_created", "login_ok"])

    def test_wrong_password_and_unknown_login_look_the_same(self):
        login = self.make_user("admin", login="boss")
        wrong = self.login(login, "not the password at all")
        unknown = self.login("nobody-here")
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.json(), unknown.json())
        self.assertEqual(wrong.headers["www-authenticate"], unknown.headers["www-authenticate"])
        self.assertEqual(self.login("BOSS ").status_code, 200)  # логин без учёта регистра и пробелов по краям

    def test_login_format_errors_are_400(self):
        for body in ({}, {"login": "a"}, {"password": "x" * 20}, {"login": 1, "password": 2}, {"login": "", "password": "x"},
                     {"login": "a" * 101, "password": "x"}, {"login": "a", "password": "x" * 257}, []):
            response = self.call("POST", "/api/v1/auth/login", json=body)
            self.assertEqual((response.status_code, response.json()["error_code"]), (400, "VALIDATION_ERROR"), body)

    def test_lockout_after_five_failures_and_unlock_by_time(self):
        login = self.make_user("admin", login="boss")
        for _ in range(5):
            self.assertEqual(self.login(login, "wrong password 123").status_code, 401)
        self.assertEqual(self.login(login).status_code, 401)  # верный пароль, но вход заблокирован
        self.now += timedelta(minutes=14, seconds=59)
        self.assertEqual(self.login(login).status_code, 401)
        self.now += timedelta(seconds=2)
        self.assertEqual(self.login(login).status_code, 200)
        self.assertEqual(self.events().count("login_locked"), 1)

    def test_successful_login_resets_failure_counter(self):
        login = self.make_user("admin", login="boss")
        for _ in range(2):
            for _ in range(4):
                self.login(login, "wrong password 123")
            self.assertEqual(self.login(login).status_code, 200)

    def test_disabled_user_cannot_log_in_and_loses_access_at_once(self):
        self.make_user("admin", login="second-admin")  # последнего администратора закрыть нельзя
        login = self.make_user("admin", login="boss")
        token = self.login(login).json()["access_token"]
        self.assertEqual(self.status_with(token), 200)
        user_service.disable_user(self.store, login, self.now)
        self.assertEqual(self.status_with(token), 401)  # не через 15 минут, а сразу
        self.assertEqual(self.login(login).status_code, 401)

    def test_role_change_invalidates_old_tokens(self):
        login = self.make_user("waiter", "m_001", "e_001")
        token = self.login(login).json()["access_token"]
        self.assertEqual(self.status_with(token, f"/api/v1/reports/tips?employee_id=e_001&{self.REPORT}"), 200)
        with self.engine.begin() as c:
            c.execute(text("UPDATE users SET role = 'merchant', employee_id = NULL WHERE login = :l"), {"l": login})
        self.assertEqual(self.status_with(token, f"/api/v1/reports/tips?employee_id=e_001&{self.REPORT}"), 401)

    # ---- refresh-токены ----

    def test_refresh_rotates_and_old_token_stops_working(self):
        first = self.login(self.make_user("admin", login="boss")).json()
        self.now += timedelta(minutes=5)
        second = self.refresh(first["refresh_token"]).json()
        self.assertNotEqual(first["refresh_token"], second["refresh_token"])
        self.assertEqual(self.status_with(second["access_token"]), 200)
        self.assertEqual(self.refresh(second["refresh_token"]).status_code, 200)

    def test_reuse_of_old_refresh_token_revokes_the_whole_chain(self):
        first = self.login(self.make_user("admin", login="boss")).json()
        second = self.refresh(first["refresh_token"]).json()
        self.now += timedelta(seconds=30)  # дольше окна повтора: это уже не сетевой повтор, а кража
        self.assertEqual(self.refresh(first["refresh_token"]).status_code, 401)
        self.assertEqual(self.refresh(second["refresh_token"]).status_code, 401)  # цепочка отозвана целиком
        self.assertIn("refresh_reuse", self.events())

    def test_quick_resend_of_refresh_does_not_kill_the_chain(self):
        first = self.login(self.make_user("admin", login="boss")).json()
        second = self.refresh(first["refresh_token"]).json()
        self.now += timedelta(seconds=3)
        self.assertEqual(self.refresh(first["refresh_token"]).status_code, 401)  # ответ потерян: нужен новый вход или токен
        self.assertEqual(self.refresh(second["refresh_token"]).status_code, 200)  # но выданный токен жив

    def test_refresh_token_expires_after_14_days(self):
        first = self.login(self.make_user("admin", login="boss")).json()
        self.now += timedelta(days=14, seconds=1)
        self.assertEqual(self.refresh(first["refresh_token"]).status_code, 401)

    def test_chain_cannot_outlive_30_days_without_new_login(self):
        token = self.login(self.make_user("admin", login="boss")).json()["refresh_token"]
        for _ in range(2):
            self.now += timedelta(days=13)
            response = self.refresh(token)
            self.assertEqual(response.status_code, 200)
            token = response.json()["refresh_token"]
        self.now += timedelta(days=5)  # день 31 от входа
        self.assertEqual(self.refresh(token).status_code, 401)

    def test_logout_revokes_chain_and_never_leaks(self):
        pair = self.login(self.make_user("admin", login="boss")).json()
        for token in (pair["refresh_token"], pair["refresh_token"], "x" * 40, "коротко"):
            response = self.call("POST", "/api/v1/auth/logout", json={"refresh_token": token})
            self.assertEqual(response.status_code, 204)
        self.assertEqual(self.refresh(pair["refresh_token"]).status_code, 401)
        self.assertEqual(self.call("POST", "/api/v1/auth/logout", json={}).status_code, 400)

    def test_password_change_revokes_all_sessions(self):
        login = self.make_user("admin", login="boss")
        pair = self.login(login).json()
        user_service.set_password(self.store, self.passwords, login, "a brand new passphrase", self.now)
        self.assertEqual(self.refresh(pair["refresh_token"]).status_code, 401)
        self.assertEqual(self.login(login).status_code, 401)
        self.assertEqual(self.login(login, "a brand new passphrase").status_code, 200)
        self.assertIn("password_changed", self.events())

    def test_refresh_of_disabled_user_fails_and_kills_chain(self):
        login = self.make_user("admin", login="boss")
        pair = self.login(login).json()
        with self.engine.begin() as c:
            c.execute(text("UPDATE users SET status = 'DISABLED' WHERE login = :l"), {"l": login})
        self.assertEqual(self.refresh(pair["refresh_token"]).status_code, 401)

    def test_tokens_are_not_interchangeable(self):
        pair = self.login(self.make_user("admin", login="boss")).json()
        self.assertEqual(self.status_with(pair["refresh_token"]), 401)  # refresh как access
        self.assertEqual(self.refresh(pair["access_token"]).status_code, 401)  # access как refresh
        self.assertEqual(self.refresh("0" * 43).status_code, 401)
        self.assertEqual(self.call("POST", "/api/v1/auth/refresh", json={}).status_code, 401)

    def test_only_hashes_are_stored(self):
        login = self.make_user("admin", login="boss")
        pair = self.login(login).json()
        row = self.user_row(login)
        self.assertTrue(row["password_hash"].startswith("$argon2"))
        self.assertNotIn(PASSWORD, row["password_hash"])
        with self.engine.connect() as c:
            stored = bytes(c.execute(text("SELECT token_hash FROM refresh_tokens")).scalar())
            dump = (str(c.execute(text("SELECT * FROM refresh_tokens")).all())
                    + str(c.execute(text("SELECT * FROM auth_events")).all()))
        self.assertEqual(stored, hashlib.sha256(pair["refresh_token"].encode()).digest())
        self.assertNotIn(pair["refresh_token"], dump)
        self.assertNotIn(PASSWORD, dump)

    def test_default_hasher_is_argon2id(self):
        self.assertTrue(Passwords().hash("a long enough passphrase").startswith("$argon2id$"))

    def test_audit_trail_has_no_secrets_and_cannot_be_edited(self):
        login = self.make_user("admin", login="boss")
        self.login(login, "wrong password 123")
        self.login(login)
        self.assertEqual(self.events(), ["user_created", "login_failed", "login_ok"])
        with self.assertRaises(Exception):
            with self.engine.begin() as c:
                c.execute(text("DELETE FROM auth_events"))

    # ---- JWT: атаки ----

    def test_forged_and_malformed_tokens_are_rejected(self):
        good = self.good_claims()
        public = self.public_pem.encode()
        header = b64(json.dumps({"alg": "HS256", "kid": KID, "typ": "JWT"}).encode())
        payload = b64(json.dumps(good).encode())
        hs256 = f"{header}.{payload}.{b64(hmac.new(public, f'{header}.{payload}'.encode(), hashlib.sha256).digest())}"
        other_private, _ = generate_key_pem()
        valid = self.signed(good)
        h, p, s = valid.split(".")
        tampered = f"{h}.{b64(json.dumps(dict(good, role='admin', merchant_id='m_002')).encode())}.{s}"
        cases = {
            "alg none": jwt.encode(good, None, algorithm="none", headers={"kid": KID}),
            "HS256 с открытым ключом как секретом": hs256,
            "чужой ключ с тем же kid": self.signed(good, key=other_private),
            "неизвестный kid": self.signed(good, kid="k-unknown"),
            "подменённые claims, подпись прежняя": tampered,
            "нет kid": jwt.encode(good, self.private_pem, algorithm="ES256"),
            "чужая аудитория": self.signed(dict(good, aud="other-api")),
            "чужой издатель": self.signed(dict(good, iss="evil")),
            "нет jti": self.signed({k: v for k, v in good.items() if k != "jti"}),
            "нет exp": self.signed({k: v for k, v in good.items() if k != "exp"}),
            "нет sub": self.signed({k: v for k, v in good.items() if k != "sub"}),
            "exp не число": self.signed(dict(good, exp="завтра")),
            "несуществующий пользователь": self.signed(dict(good, sub="u_0000000000000000")),
            "роль не совпадает с базой": self.signed(dict(good, role="merchant", merchant_id="m_001")),
            "мусор": "a.b.c",
            "пусто": "x",
        }
        for name, token in cases.items():
            self.assertEqual(self.status_with(token), 401, name)
        self.assertEqual(self.status_with(valid), 200)  # контроль: настоящий токен проходит

    def test_expired_and_future_tokens(self):
        token = self.login(self.make_user("admin", login="boss")).json()["access_token"]
        self.assertEqual(self.status_with(token), 200)
        self.now += timedelta(minutes=15, seconds=6)
        self.assertEqual(self.status_with(token), 401)  # 15 минут и допуск 5 секунд прошли
        self.now = self.now - timedelta(hours=2)
        self.assertEqual(self.status_with(token), 401)  # токен «из будущего» относительно часов сервера

    def test_authorization_header_forms(self):
        token = self.login(self.make_user("admin", login="boss")).json()["access_token"]
        path = f"/api/v1/reports/tips?merchant_id=m_001&{self.REPORT}"
        for header, expected in ((f"Bearer {token}", 200), (f"bearer {token}", 200), (f"Bearer  {token}", 200),
                                 (token, 401), (f"Token {token}", 401), ("Bearer", 401), ("Bearer ", 401)):
            self.assertEqual(self.client.get(path, headers={"Authorization": header}).status_code, expected, header)

    def test_key_rotation_without_downtime(self):
        token = self.login(self.make_user("admin", login="boss")).json()["access_token"]
        new_private, _ = generate_key_pem()

        def status_under(keyring):
            app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, keyring, self.passwords)
            with TestClient(app) as client:
                return client.get(f"/api/v1/reports/tips?merchant_id=m_001&{self.REPORT}",
                                  headers=self.bearer(token)).status_code

        self.assertEqual(status_under(Keyring("k-new", new_private, {KID: self.public_pem})), 200)  # старый ключ ещё принимается
        self.assertEqual(status_under(Keyring("k-new", new_private)), 401)  # после вывода старого ключа токен недействителен
        fresh = Keyring("k-new", new_private, {KID: self.public_pem}).sign(self.claims_of(token))
        self.assertEqual(jwt.get_unverified_header(fresh)["kid"], "k-new")  # подписывает уже новый ключ

    def test_keyring_refuses_bad_keys(self):
        import cryptography.hazmat.primitives.asymmetric.rsa as rsa
        from cryptography.hazmat.primitives import serialization
        rsa_pem = rsa.generate_private_key(65537, 2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        with self.assertRaises(ValueError):
            Keyring("k", rsa_pem)
        private, public = generate_key_pem()
        with self.assertRaises(ValueError):
            Keyring("k", private, {"k": public})

    # ---- замечания ревью: гонки, ревокация, затухание, перегрузка ----

    def test_concurrent_refresh_of_one_token_gives_one_pair_and_keeps_the_chain(self):
        pair = self.login(self.make_user("admin", login="boss")).json()
        results, barrier = [], threading.Barrier(6)

        def worker():
            with TestClient(self.app) as client:
                barrier.wait()
                results.append(self.call("POST", "/api/v1/auth/refresh", client=client,
                                         json={"refresh_token": pair["refresh_token"]}))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r.status_code for r in results), [200] + [401] * 5)
        winner = next(r for r in results if r.status_code == 200).json()
        self.assertEqual(self.refresh(winner["refresh_token"]).status_code, 200)  # цепочка не отозвана «лишними» запросами

    def test_refresh_waits_for_user_lock_so_password_change_cannot_miss_a_new_token(self):
        login = self.make_user("admin", login="boss")
        pair = self.login(login).json()
        user_id = self.user_row(login)["user_id"]
        outcome = []
        holder = self.store.__class__(self.engine)
        with holder.atomic():
            holder.lock_user(user_id)  # так выглядит смена пароля между блокировкой и отзывом
            thread = threading.Thread(target=lambda: outcome.append(self.refresh(pair["refresh_token"]).status_code))
            thread.start()
            time.sleep(0.4)
            self.assertTrue(thread.is_alive(), "обновление не должно проходить мимо блокировки пользователя")
            holder.revoke_user_tokens(user_id, self.now)
        thread.join(10)
        self.assertEqual(outcome, [401])  # после отзыва нового токена не появилось
        with self.engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")).scalar(), 0)

    def test_password_changed_while_login_verifies_does_not_log_in_and_is_not_rolled_back(self):
        login = self.make_user("admin", login="boss")
        test = self

        class Racing(Passwords):
            def verify(self, password_hash, password):
                ok = super().verify(password_hash, password)
                if ok and not getattr(self, "done", False):
                    self.done = True  # пароль сменился между проверкой и выдачей токенов
                    user_service.set_password(test.store, self, login, "a brand new passphrase", test.now)
                return ok

        racing = Racing(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
        with self.assertRaises(auth_service.Unauthenticated):
            auth_service.login(self.store, self.keyring, racing, login, PASSWORD, self.now)
        self.assertEqual(self.login(login, "a brand new passphrase").status_code, 200)

    def test_hash_is_upgraded_at_login_without_changing_the_password(self):
        login = self.make_user("admin", login="boss")
        before = self.user_row(login)
        self.passwords = Passwords(PasswordHasher(time_cost=2, memory_cost=16, parallelism=1))  # параметры ужесточили
        self.app = create_app(self.engine, self.gateway, SECRET, lambda: self.now, self.keyring, self.passwords)
        self.client = TestClient(self.app)
        self.assertEqual(self.login(login).status_code, 200)
        after = self.user_row(login)
        self.assertNotEqual(before["password_hash"], after["password_hash"])
        self.assertIn("t=2", after["password_hash"])
        self.assertEqual(before["password_changed_at"], after["password_changed_at"])  # это не смена пароля
        self.assertNotIn("password_changed", self.events())
        self.assertEqual(self.login(login).status_code, 200)

    def test_access_token_issued_before_password_change_is_rejected(self):
        login = self.make_user("admin", login="boss")
        old = self.login(login).json()["access_token"]
        self.assertEqual(self.status_with(old), 200)
        self.now += timedelta(minutes=1)
        user_service.set_password(self.store, self.passwords, login, "a brand new passphrase", self.now)
        self.assertEqual(self.status_with(old), 401)  # украденный до смены пароля токен не доживает до 15 минут
        fresh = self.login(login, "a brand new passphrase").json()["access_token"]
        self.assertEqual(self.status_with(fresh), 200)

    def test_overload_of_password_hashing_returns_429(self):
        login = self.make_user("admin", login="boss")
        slots = [self.passwords._guard() for _ in range(4)]  # заняты все места хеширования
        try:
            with mock.patch("tips.adapters.passwords.WAIT_SECONDS", 0.05):
                for who in (login, "no-such-login"):  # поток логинов не может исчерпать память и процессор
                    response = self.call("POST", "/api/v1/auth/login", json={"login": who, "password": PASSWORD})
                    self.assertEqual((response.status_code, response.json()["error_code"]), (429, "RATE_LIMITED"))
                    self.assertEqual(response.headers["retry-after"], "1")
        finally:
            for slot in slots:
                slot.release()
        self.assertEqual(self.login(login).status_code, 200)

    def test_failed_attempts_decay_with_time(self):
        login = self.make_user("admin", login="boss")
        for _ in range(4):
            self.login(login, "wrong password 123")
        self.now += timedelta(minutes=16)  # прошло больше окна блокировки: счёт идёт заново
        for _ in range(4):
            self.login(login, "wrong password 123")
        self.assertEqual(self.login(login).status_code, 200)
        self.assertNotIn("login_locked", self.events())

    def test_unknown_login_attempts_are_logged_without_the_login(self):
        self.login("someone-unknown")
        with self.engine.connect() as c:
            rows = c.execute(text("SELECT user_id, action FROM auth_events")).all()
        self.assertEqual([tuple(r) for r in rows], [(None, "login_failed")])

    def test_admin_action_is_attributed_to_a_person(self):
        login = self.make_user("admin", login="boss")
        token = self.login(login).json()["access_token"]
        self.call("PATCH", "/api/v1/merchants/m_001/status", headers=self.bearer(token),
                  json={"status": "BLOCKED", "reason": "проверка"})
        user_id = self.user_row(login)["user_id"]
        self.assertEqual(self.count("SELECT actor FROM merchant_status_events"), f"admin:{user_id}")

    def test_last_admin_cannot_be_disabled_and_users_can_be_enabled_again(self):
        only = self.make_user("admin", login="boss")
        with self.assertRaises(user_service.UserError):
            user_service.disable_user(self.store, only, self.now)
        second = self.make_user("admin", login="second-admin")
        user_service.disable_user(self.store, only, self.now)
        self.assertEqual(self.login(only).status_code, 401)
        with self.assertRaises(user_service.UserError):
            user_service.disable_user(self.store, second, self.now)  # теперь последний действующий
        user_service.enable_user(self.store, only, self.now)
        self.assertEqual(self.login(only).status_code, 200)
        self.assertIn("user_enabled", self.events())

    def test_password_policy_and_unicode_normalization(self):
        for weak in ("passwordpassword", "abcabcabcabc", "Qwerty-is-my-key-12", "Welcome to the jungle"):
            with self.assertRaises(user_service.UserError, msg=weak):
                user_service.create_user(self.store, self.passwords, "someone", weak, "admin", None, None, self.now)
        fullwidth = "Ｃorrect horse battery staple"  # то же слово, набранное полноширинной буквой
        self.make_user("admin", login="boss", password=fullwidth)
        self.assertEqual(self.login("boss", "Correct horse battery staple").status_code, 200)

    def test_oversized_and_odd_bearer_values_never_cause_500(self):
        good = self.good_claims()
        def forged(kid):  # PyJWT не даёт выпустить такой заголовок, поэтому собираем токен вручную
            header = b64(json.dumps({"alg": "ES256", "kid": kid, "typ": "JWT"}).encode())
            return f"{header}.{b64(json.dumps(good).encode())}.{b64(b'x' * 64)}"

        odd_kid, list_kid = forged(123), forged([KID])
        for token in ("A" * 1_000_000, odd_kid, list_kid, "..", "a.b", "e30.e30.e30"):
            self.assertEqual(self.status_with(token), 401, token[:30])


    # ---- роль × операция ----

    def test_role_matrix(self):
        qr = lambda m, e, r: ("POST", "/api/v1/qr", {"json": {"merchant_id": m, "employee_id": e, "receipt_id": r}})  # noqa: E731
        q = self.REPORT
        operations = {
            "QR на e_001": (qr("m_001", "e_001", "r_1001"), (201, 403, 201, 403, 403)),
            "QR на e_002": (qr("m_001", "e_002", "r_1002"), (201, 403, 403, 201, 403)),
            "QR чужого заведения": (qr("m_002", "e_004", "r_1004"), (403, 201, 403, 403, 403)),
            "отчёт m_001": (("GET", f"/api/v1/reports/tips?merchant_id=m_001&{q}", {}), (200, 403, 403, 403, 200)),
            "отчёт m_002": (("GET", f"/api/v1/reports/tips?merchant_id=m_002&{q}", {}), (403, 200, 403, 403, 200)),
            "отчёт e_001": (("GET", f"/api/v1/reports/tips?employee_id=e_001&{q}", {}), (403, 403, 200, 403, 200)),
            "отчёт e_002": (("GET", f"/api/v1/reports/tips?employee_id=e_002&{q}", {}), (403, 403, 403, 200, 200)),
            "по дням m_001": (("GET", f"/api/v1/reports/tips/daily?merchant_id=m_001&{q}", {}), (200, 403, 403, 403, 200)),
            "по дням m_002": (("GET", f"/api/v1/reports/tips/daily?merchant_id=m_002&{q}", {}), (403, 200, 403, 403, 200)),
            "статус m_001": (("PATCH", "/api/v1/merchants/m_001/status", {"json": {"status": "ACTIVE", "reason": "проверка"}}),
                             (403, 403, 403, 403, 200)),
        }
        users = [("мерчант m_001", self.as_user("merchant", "m_001")), ("мерчант m_002", self.as_user("merchant", "m_002")),
                 ("официант e_001", self.as_user("waiter", "m_001", "e_001")),
                 ("официант e_002", self.as_user("waiter", "m_001", "e_002")), ("администратор", self.as_user("admin"))]
        for name, ((method, path, kwargs), expected) in operations.items():
            anonymous = self.call(method, path, **kwargs)
            self.assertEqual(anonymous.status_code, 401, f"{name}: без входа")
            for (who, headers), want in zip(users, expected):
                response = self.call(method, path, headers=headers, **kwargs)
                self.assertEqual(response.status_code, want, f"{name}: {who}")
                if want == 403:
                    self.assertEqual(response.json()["error_code"], "ACCESS_DENIED")

    def test_identity_comes_from_token_not_from_request(self):
        """Подставить чужие идентификаторы в запрос и заголовки нельзя: владелец определяется токеном."""
        waiter = self.as_user("waiter", "m_001", "e_001")
        spoofed = dict(waiter, **{"X-Dev-Role": "admin", "X-Dev-Merchant": "m_002", "X-Dev-Employee": "e_002"})
        response = self.call("GET", f"/api/v1/reports/tips?employee_id=e_002&{self.REPORT}", headers=spoofed)
        self.assertEqual(response.status_code, 403)
        response = self.call("PATCH", "/api/v1/merchants/m_001/status", headers=spoofed,
                             json={"status": "BLOCKED", "reason": "захват"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.merchant_status("m_001"), "ACTIVE")

    # ---- учётные записи ----

    def test_user_creation_rules(self):
        make = lambda **kw: user_service.create_user(  # noqa: E731
            self.store, self.passwords, kw.get("login", "someone"), kw.get("password", PASSWORD), kw.get("role", "admin"),
            kw.get("merchant"), kw.get("employee"), self.now)
        bad = [dict(password="short"), dict(password="a" * 129), dict(login="Ab"), dict(login="has space"),
               dict(login="someone", password="someone" * 2), dict(password="aaaaaaaaaaaaa"),
               dict(role="root"), dict(role="admin", merchant="m_001"), dict(role="merchant"),
               dict(role="merchant", merchant="m_001", employee="e_001"), dict(role="waiter", merchant="m_001"),
               dict(role="waiter", merchant="m_002", employee="e_001"),    # официант чужого заведения
               dict(role="merchant", merchant="m_777")]
        for kwargs in bad:
            with self.assertRaises(user_service.UserError, msg=str(kwargs)):
                make(**kwargs)
        self.assertEqual(self.count("SELECT count(*) FROM users"), 0)
        make(login="first", role="waiter", merchant="m_001", employee="e_001")
        with self.assertRaises(ValueError):
            make(login="first")  # логин занят
        with self.assertRaises(ValueError):
            make(login="second", role="waiter", merchant="m_001", employee="e_001")  # у официанта уже есть учётная запись

    def test_auth_service_checks_without_http(self):
        self.make_user("admin", login="boss")
        pair = auth_service.login(self.store, self.keyring, self.passwords, "boss", PASSWORD, self.now)
        principal = auth_service.authenticate(self.store, self.keyring, pair["access_token"], self.now)
        self.assertEqual((principal.role, principal.merchant_id, principal.employee_id), ("admin", None, None))


if __name__ == "__main__":
    unittest.main()
