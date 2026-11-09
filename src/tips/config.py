"""Пути и секрет. Секрет только из окружения, без значения по умолчанию (ADR-009)."""
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROTOTYPE_DIR = os.path.join(REPO_ROOT, "prototype")
DATA_DIR = os.path.join(PROTOTYPE_DIR, "01_данные")
SCENARIOS_CSV = os.path.join(REPO_ROOT, "data", "gateway_scenarios.csv")
PAY_URL = "https://tips.example/pay"


def get_secret():
    """ADR-009: без значения по умолчанию, нет переменной — нет запуска."""
    value = os.environ.get("TIPS_HMAC_SECRET")
    if not value:
        sys.exit("Ошибка: не задана переменная окружения TIPS_HMAC_SECRET")
    return value.encode()
