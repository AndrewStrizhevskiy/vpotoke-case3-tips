"""Файлы развёртывания: скрипты разбираются оболочкой, образ без root,
сompose и пример окружения согласованы с проверками production.

    python3 -m unittest tests.test_deploy
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from tips.http import prodcheck  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DeployTests(unittest.TestCase):
    def test_shell_scripts_parse_and_are_executable(self):
        folder = os.path.join(ROOT, "deploy")
        scripts = [n for n in os.listdir(folder) if n.endswith(".sh")]
        self.assertEqual(sorted(scripts), ["backup.sh", "restore.sh", "verify.sh"])
        for name in scripts:
            path = os.path.join(folder, name)
            self.assertTrue(os.access(path, os.X_OK), name)
            self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0, name)
            self.assertIn("set -eu", read("deploy", name), name)

    def test_image_runs_as_non_root_with_a_healthcheck_and_no_secrets(self):
        text = read("Dockerfile")
        self.assertRegex(text, r"(?m)^USER tips$")
        self.assertIn("HEALTHCHECK", text)
        self.assertNotRegex(text, r"(?i)password|secret=|private_key=")
        ignore = read(".dockerignore")
        for entry in (".git", ".env*", ".venv"):
            self.assertIn(entry, ignore)

    def test_compose_hardens_services_and_requires_a_database_password(self):
        text = read("docker-compose.yml")
        self.assertIn("POSTGRES_PASSWORD:?", text)  # без пароля compose не стартует
        self.assertIn('"127.0.0.1:8083:8083"', text)  # наружу только через локальный прокси
        self.assertEqual(text.count("read_only: true"), 2)
        self.assertEqual(text.count("cap_drop: [ALL]"), 2)
        self.assertEqual(text.count("no-new-privileges"), 2)

    def test_env_example_matches_production_checks_and_holds_no_real_secrets(self):
        text = read("deploy", "env.example")
        values = dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))
        self.assertEqual(values["TIPS_API_V1"], "off")
        self.assertTrue(values["TIPS_FRONT_URL"].startswith("https://"))
        for name in ("TIPS_QR_KEYS", "TIPS_JWT_PRIVATE_KEY", "TIPS_METRICS_TOKEN"):
            self.assertEqual(values[name], "", name)  # реальных ключей в примере нет
        self.assertIsNone(re.search(r"BEGIN [A-Z ]*PRIVATE KEY", text))
        # пример без секретов обязан быть отвергнут проверками production: пустые ключи не пройдут
        env = {**values, "TIPS_DATABASE_URL": "postgresql://x@h/db"}
        self.assertTrue(prodcheck.problems(env))

    def test_gitignore_keeps_the_real_env_file_out_of_git(self):
        self.assertIn(".env.*", read(".gitignore"))
        self.assertIn(".env", read(".gitignore"))


if __name__ == "__main__":
    unittest.main()
