"""Песочный провайдер: ведёт себя как настоящий (токен карты, 3-D Secure, вебхуки, возврат), чтобы весь поток
строился и проверялся до выбора реального провайдера. В production запрещён (см. config.check_production).

Тестовые карты (последние цифры номера):
    4242  успех сразу            0002  отказ (CARD_DECLINED)
    3220  нужна проверка 3-D Secure (ACTION_REQUIRED)
    0119  «таймаут»: банк списал деньги, но ответа нет (PROCESSING); итог узнаётся через settle() и вебхук
Номер карты в нашем коде не появляется: форма провайдера отдаёт только токен, а в песочнице токен получает
тестовый код через SandboxBank.create_token().
"""
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone

from tips.adapters.provider.base import (ACTION_REQUIRED, FAILED, NOT_FOUND, PROCESSING, SUCCEEDED, PaymentProvider,
                                         ProviderEvent, ProviderResult, ProviderUnavailable, RefundResult, WebhookInvalid)

TOLERANCE_SECONDS = 300
CARDS = {"4242": "success", "0002": "decline", "3220": "challenge", "0119": "timeout"}


def sign_webhook(secret, body, timestamp):
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Sandbox-Signature": f"t={timestamp},v1={mac}"}


class SandboxBank:
    """Банк в памяти: потокобезопасный автомат платежей, токенов, возвратов и очереди вебхуков."""

    def __init__(self, webhook_secret="whsec_sandbox_for_tests", clock=None):
        self.webhook_secret = webhook_secret
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.tokens, self.payments, self.by_reference, self.refunds, self.refunds_by_key = {}, {}, {}, {}, {}
        self.events, self.charge_calls, self._fail_next = [], 0, {}

    # ---- токены карты (форма провайдера) ----

    def create_token(self, pan):
        digits = "".join(ch for ch in pan if ch.isdigit())
        with self._lock:
            token = "tok_" + secrets.token_hex(12)
            self.tokens[token] = CARDS.get(digits[-4:], "decline")
            return token

    # ---- управление в тестах ----

    def fail_next(self, operation, times=1):
        with self._lock:
            self._fail_next[operation] = times

    def _maybe_fail(self, operation):
        with self._lock:
            if self._fail_next.get(operation, 0) > 0:
                self._fail_next[operation] -= 1
                raise ProviderUnavailable(operation)

    def _emit(self, event_type, payment_id, extra=None):
        self.events.append({"id": "evt_" + secrets.token_hex(8), "type": event_type, "payment_id": payment_id, **(extra or {})})

    def complete_challenge(self, provider_payment_id, ok=True):
        with self._lock:
            p = self.payments[provider_payment_id]
            if p["state"] != "challenge":
                return
            p["state"] = "succeeded" if ok else "failed"
            p["error"] = None if ok else "CARD_DECLINED"
            self._emit("payment.succeeded" if ok else "payment.failed", provider_payment_id)

    def settle(self, provider_payment_id, ok=True):
        """Банк наконец ответил на «потерянный» платёж (карта 0119)."""
        with self._lock:
            p = self.payments[provider_payment_id]
            if p["state"] != "processing":
                return
            p["state"] = "succeeded" if ok else "failed"
            p["error"] = None if ok else "CARD_DECLINED"
            self._emit("payment.succeeded" if ok else "payment.failed", provider_payment_id)

    def drain_webhooks(self, duplicate=False, reverse=False):
        """Очередь событий в виде пар (заголовки, тело) для вебхука; duplicate повторяет, reverse меняет порядок."""
        with self._lock:
            events, self.events = self.events, []
        if reverse:
            events = list(reversed(events))
        out = []
        for ev in events:
            body = json.dumps(ev, separators=(",", ":")).encode()
            stamp = int(self.clock().timestamp())
            for _ in range(2 if duplicate else 1):
                out.append((sign_webhook(self.webhook_secret, body, stamp), body))
        return out

    # ---- операции провайдера ----

    def create_payment(self, reference, amount, currency, token, return_url, idempotency_key):
        self._maybe_fail("create_payment")
        with self._lock:
            self.charge_calls += 1
            existing = self.by_reference.get(reference)
            if existing:  # повтор по тому же ключу: тот же платёж, второго списания нет
                return self._result(self.payments[existing])
            behaviour = self.tokens.get(token)
            if behaviour is None:
                return ProviderResult(FAILED, None, None, "INVALID_PAYMENT_METHOD")
            pid = "sb_" + secrets.token_hex(10)
            state, error = {"success": ("succeeded", None), "decline": ("failed", "CARD_DECLINED"),
                            "challenge": ("challenge", None), "timeout": ("processing", None)}[behaviour]
            self.payments[pid] = {"id": pid, "reference": reference, "amount": str(amount), "currency": currency,
                                  "state": state, "error": error, "refunded": False, "return_url": return_url}
            self.by_reference[reference] = pid
            if state in ("succeeded", "failed"):
                self._emit("payment.succeeded" if state == "succeeded" else "payment.failed", pid)
            result = self._result(self.payments[pid])
            if behaviour == "timeout":  # банк принял и списал, а ответ клиенту не дошёл
                raise ProviderUnavailable("timeout after charge")
            return result

    def _result(self, p):
        state, ref = p["state"], p["reference"]
        if state == "succeeded":
            return ProviderResult(SUCCEEDED, p["id"], reference=ref)
        if state == "failed":
            return ProviderResult(FAILED, p["id"], None, p["error"], ref)
        if state == "challenge":
            return ProviderResult(ACTION_REQUIRED, p["id"], f"https://sandbox.bank.example/3ds/{p['id']}", reference=ref)
        if state == "voided":
            return ProviderResult(FAILED, p["id"], None, "CANCELED", ref)
        return ProviderResult(PROCESSING, p["id"], reference=ref)

    def get_status(self, reference=None, provider_payment_id=None):
        self._maybe_fail("get_status")
        with self._lock:
            pid = provider_payment_id or self.by_reference.get(reference)
            p = self.payments.get(pid)
            return ProviderResult(NOT_FOUND) if p is None else self._result(p)

    def void(self, provider_payment_id):
        with self._lock:
            p = self.payments.get(provider_payment_id)
            if p is None or p["state"] == "succeeded":
                return p is None  # неизвестный платёж отменять нечего; списанный отменить нельзя (только возврат)
            if p["state"] in ("challenge", "processing"):
                p["state"] = "voided"
            return True

    def refund(self, provider_payment_id, amount, idempotency_key):
        self._maybe_fail("refund")
        with self._lock:
            if idempotency_key in self.refunds_by_key:
                return self.refunds_by_key[idempotency_key]
            p = self.payments.get(provider_payment_id)
            if p is None or p["state"] != "succeeded" or p["refunded"]:
                return RefundResult("FAILED")
            p["refunded"] = True
            result = RefundResult("SUCCEEDED", "rfn_" + secrets.token_hex(8))
            self.refunds_by_key[idempotency_key] = result
            self._emit("refund.succeeded", provider_payment_id, {"refund_id": result.provider_refund_id})
            return result


class SandboxProvider(PaymentProvider):
    name = "sandbox"
    action_hosts = ("sandbox.bank.example",)

    def __init__(self, bank=None):
        self.bank = bank or SandboxBank()

    def create_payment(self, reference, amount, currency, payment_method_token, return_url, idempotency_key):
        return self.bank.create_payment(reference, amount, currency, payment_method_token, return_url, idempotency_key)

    def get_status(self, reference=None, provider_payment_id=None):
        return self.bank.get_status(reference, provider_payment_id)

    def void(self, provider_payment_id):
        return self.bank.void(provider_payment_id)

    def refund(self, provider_payment_id, amount, idempotency_key):
        return self.bank.refund(provider_payment_id, amount, idempotency_key)

    def parse_webhook(self, headers, raw_body, now):
        header = next((v for k, v in headers.items() if k.lower() == "sandbox-signature"), "")
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p) if header else {}
        try:
            stamp = int(parts.get("t", ""))
        except ValueError:
            raise WebhookInvalid()
        if abs(now.timestamp() - stamp) > TOLERANCE_SECONDS:
            raise WebhookInvalid()
        expected = hmac.new(self.bank.webhook_secret.encode(), f"{stamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
        pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
        signatures = [v for k, v in pairs if k == "v1"]  # несколько на время ротации секрета
        if not any(hmac.compare_digest(expected, s) for s in signatures):
            raise WebhookInvalid()
        try:
            data = json.loads(raw_body)
            return ProviderEvent(str(data["id"]), str(data["type"]), data.get("payment_id"), data)
        except (ValueError, KeyError, TypeError):
            raise WebhookInvalid()
