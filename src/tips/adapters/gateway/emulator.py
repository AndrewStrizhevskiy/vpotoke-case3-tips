"""Эмулятор шлюза (ADR-001): таблица сценариев приоритетнее формулы организаторов."""
import hashlib

from tips.adapters.csv_store import read_csv
from tips.config import SCENARIOS_CSV


def load_scenarios(path=SCENARIOS_CSV):
    """ADR-001: явные тестовые исходы, как тестовые карты в sandbox шлюза."""
    return {r["qr_id"]: (r["result"], r["error_code"] or None) for r in read_csv(path)}


def emulate_gateway(qr_id, nonce, scenarios):
    """ADR-001: таблица сценариев приоритетнее формулы организаторов."""
    if qr_id in scenarios:
        result, error = scenarios[qr_id]
        return result, error
    h = int(hashlib.md5((qr_id + nonce).encode()).hexdigest(), 16) % 10
    if h <= 7:
        return "SUCCESS", None
    if h == 8:
        return "DECLINED", "CARD_DECLINED"
    return "PENDING", "GATEWAY_TIMEOUT"


class EmulatorGateway:
    """Реализация PaymentGateway поверх эмулятора; таблица сценариев читается при каждом вызове."""

    def __init__(self, scenarios_csv=SCENARIOS_CSV):
        self.scenarios_csv = scenarios_csv

    def charge(self, qr_id, nonce):
        return emulate_gateway(qr_id, nonce, load_scenarios(self.scenarios_csv))
