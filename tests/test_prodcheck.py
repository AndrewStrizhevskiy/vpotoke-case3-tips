"""Проверки конфигурации production, генератор ключей QR v2 и переключатель TIPS_API_V1.

    python3 -m unittest tests.test_prodcheck
"""
import base64
import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from tips.http import keys, prodcheck  # noqa: E402

GOOD = {
    "TIPS_ENV": "production", "TIPS_HMAC_SECRET": "x" * 32, "TIPS_API_V1": "off",
    "TIPS_QR_KEYS": "k1:" + base64.b64encode(bytes(32)).decode(), "TIPS_QR_ACTIVE_KID": "k1",
    "TIPS_FRONT_URL": "https://tips.example/p", "TIPS_JWT_KID": "k20261109", "TIPS_JWT_PRIVATE_KEY": "pem",
    "TIPS_DATABASE_URL": "postgresql://u@h/db", "TIPS_CORS_ORIGINS": "https://tips.example",
}


class ProdCheckTests(unittest.TestCase):
    def bad(self, **changes):
        env = {**GOOD, **changes}
        env = {k: v for k, v in env.items() if v is not None}
        return prodcheck.problems(env)

    def test_good_production_config_has_no_problems(self):
        self.assertEqual(prodcheck.problems(GOOD), [])

    def test_development_is_not_checked(self):
        self.assertEqual(prodcheck.problems({"TIPS_ENV": "development"}), [])

    def test_each_unsafe_setting_is_reported(self):
        cases = {
            "TIPS_HMAC_SECRET": "demo-secret-k3",
            "TIPS_API_V1": "on",
            "TIPS_QR_KEYS": None,
            "TIPS_QR_ACTIVE_KID": None,
            "TIPS_PROVIDER": "sandbox",
            "TIPS_FRONT_URL": "http://tips.example/p",
            "TIPS_JWT_PRIVATE_KEY": None,
            "TIPS_JWT_KID": None,
            "TIPS_DATABASE_URL": None,
            "TIPS_CORS_ORIGINS": "*",
            "TIPS_METRICS_TOKEN": "short",
        }
        for name, value in cases.items():
            with self.subTest(name):
                found = self.bad(**{name: value})
                self.assertTrue(any(name in line for line in found), (name, found))

    def test_missing_api_v1_variable_counts_as_on(self):
        self.assertTrue(self.bad(TIPS_API_V1=None))

    def test_front_url_must_point_at_p_and_kids_must_be_unique(self):
        self.assertTrue(self.bad(TIPS_FRONT_URL="https://tips.example/"))
        self.assertTrue(self.bad(TIPS_FRONT_URL="https://tips.example/pay"))
        self.assertEqual(self.bad(TIPS_FRONT_URL="https://tips.example/p/"), [])
        key = base64.b64encode(bytes(32)).decode()
        self.assertTrue(self.bad(TIPS_QR_KEYS=f"k1:{key},k1:{key}"))

    def test_weak_or_malformed_qr_keys(self):
        self.assertTrue(self.bad(TIPS_QR_KEYS="k1:" + base64.b64encode(bytes(8)).decode()))
        self.assertTrue(self.bad(TIPS_QR_KEYS="k1:не-base64"))
        long_kid = "TOOLONGKID1"
        self.assertTrue(self.bad(TIPS_QR_KEYS=long_kid + ":" + base64.b64encode(bytes(32)).decode(), TIPS_QR_ACTIVE_KID=long_kid))
        self.assertTrue(self.bad(TIPS_QR_ACTIVE_KID="k9"))  # активного ключа нет в связке

    def test_http_entrypoint_refuses_to_start_on_a_bad_config(self):
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith("TIPS_")}
        env.update({"TIPS_ENV": "production", "TIPS_HMAC_SECRET": "demo-secret-k3",
                    "PYTHONPATH": os.path.join(HERE, "..", "src")})
        run = subprocess.run([sys.executable, "-m", "tips.http"], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("небезопасная конфигурация", run.stderr)
        self.assertIn("TIPS_API_V1", run.stderr)


class KeyGeneratorTests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            keys.main(list(args))
        return out.getvalue()

    def test_qr_key_is_loadable_by_the_service_and_passes_prodcheck(self):
        from tips.domain.qr_v2 import QrV2Keys
        text = self.run_cli("qr", "q1")
        env = dict(line[len("export "):].split("=", 1) for line in text.splitlines() if line.startswith("export "))
        env = {k: v.strip('"') for k, v in env.items()}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            ring = QrV2Keys.from_env()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(ring.active_kid, "q1")
        self.assertEqual(len(ring.ring["q1"]), 32)
        self.assertEqual(prodcheck.problems({**GOOD, **env}), [])

    def test_two_runs_give_different_keys(self):
        self.assertNotEqual(self.run_cli("qr", "a"), self.run_cli("qr", "a"))

    def test_without_arguments_the_jwt_key_is_still_printed(self):
        self.assertIn("TIPS_JWT_PRIVATE_KEY", self.run_cli())


if __name__ == "__main__":
    unittest.main()
