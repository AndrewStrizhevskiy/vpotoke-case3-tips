"""Ограничение частоты запросов по адресу клиента (ADR-009). Счётчики лежат в PostgreSQL (таблица rate_hits).

Фиксированные окна: просто, предсказуемо и достаточно при небольшой нагрузке. Ключ всегда содержит адрес клиента,
а где есть что защищать, ещё и объект (qr_id, payment_id, пользователь), чтобы один шумный клиент не мешал остальным.
Адреса за общим NAT (Wi-Fi ресторана) делят лимит, поэтому лимиты на клиентские операции не слишком жёсткие,
а блокировка неверных подписей идёт по паре «адрес и qr_id», а не по адресу целиком.
"""
import ipaddress
import math
import os
from dataclasses import dataclass

from tips.domain.errors import RateLimited


@dataclass(frozen=True)
class Rule:
    limit: int
    window: int  # секунды


DEFAULT_RULES = {
    "global": Rule(300, 60),       # любой запрос с одного адреса
    "qr_view": Rule(30, 60),       # GET /qr/{id}
    "tip_create": Rule(10, 60),    # POST /tips: адрес и qr_id
    "pay_action": Rule(10, 60),    # confirm и cancel: адрес и payment_id
    "pay_status": Rule(30, 60),    # опрос статуса: адрес и payment_id
    "login": Rule(10, 60),         # вход: адрес
    "refresh": Rule(20, 60),       # обновление и выход: адрес
    "protected": Rule(120, 60),    # защищённые операции: пользователь
    "bad_sig": Rule(5, 900),       # неверные подписи: адрес и qr_id; после 5 подряд блок на окно
}


def _header_lines(request, name):
    """Все строки заголовка: прокси может добавить новую строку, а не дописать к существующей."""
    getlist = getattr(request.headers, "getlist", None)
    if getlist is not None:
        return getlist(name)
    value = request.headers.get(name)
    return [value] if value else []


def client_ip(request, proxy_hops=0):
    """Адрес клиента. За доверенным прокси берётся запись X-Forwarded-For, добавленная ближайшим к нам прокси
    (отсчёт справа: левые записи подделывает сам клиент). Некорректное значение игнорируется. IPv6 сводится к /64:
    иначе один клиент с целой подсетью обходил бы лимит сменой адреса."""
    raw = request.client.host if request.client else "unknown"
    if proxy_hops > 0:
        parts = [p.strip() for line in _header_lines(request, "x-forwarded-for") for p in line.split(",") if p.strip()]
        if len(parts) >= proxy_hops:
            raw = parts[-proxy_hops]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    address = getattr(address, "ipv4_mapped", None) or address  # ::ffff:1.2.3.4 при dual-stack это обычный IPv4-клиент
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address) + "/64"
    return str(address)


def proxy_hops_from_env():
    try:
        return max(0, int(os.environ.get("TIPS_TRUSTED_PROXY_HOPS", "0")))
    except ValueError:
        return 0


class Limiter:
    CACHE_MAX = 50_000

    def __init__(self, rules=None, scale=1):
        self.rules = dict(rules or DEFAULT_RULES)
        self.scale = scale  # тесты ослабляют лимиты множителем
        self._denied = {}  # ключ -> конец окна, до которого уже отказано: заблокированный клиент не нагружает базу записью

    def _limit(self, rule):
        return self.rules[rule].limit * self.scale

    @staticmethod
    def _retry_after(window, now):
        return window - int(now.timestamp()) % window

    def check(self, store, rule, key, now):
        """Учесть запрос; сверх лимита RateLimited с Retry-After до конца окна."""
        r = self.rules[rule]
        full, stamp = f"{rule}:{key}", now.timestamp()
        until = self._denied.get(full)
        if until is not None and until > stamp:  # в этом окне уже отказано: в базу не ходим
            raise RateLimited(math.ceil(until - stamp))
        if store.hit(full, r.window, now) > self._limit(rule):
            retry = self._retry_after(r.window, now)
            if len(self._denied) >= self.CACHE_MAX:
                self._denied = {k: v for k, v in self._denied.items() if v > stamp}
                if len(self._denied) >= self.CACHE_MAX:
                    self._denied.clear()
            self._denied[full] = stamp + retry
            raise RateLimited(retry)

    def blocked(self, store, rule, key, now):
        """Лимит уже исчерпан (без учёта нового запроса): блокировка действует."""
        r = self.rules[rule]
        if store.hits(f"{rule}:{key}", r.window, now) >= self._limit(rule):
            raise RateLimited(self._retry_after(r.window, now))

    def record(self, store, rule, key, now):
        store.hit(f"{rule}:{key}", self.rules[rule].window, now)
