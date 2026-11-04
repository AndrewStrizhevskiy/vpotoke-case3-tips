#!/usr/bin/env python3
"""
CLI-прототип платформы чаевых — кейс 3.

Команды (первый аргумент; без команды выполняется pay):
    pay      qr.json      create + confirm за один запуск
    create   qr.json      проверки -> платёж в статусе CREATED
    confirm  payment_id   CREATED -> PENDING -> итог эмулятора
    cancel   payment_id   CREATED -> CANCELED (иначе CANCEL_NOT_ALLOWED; повтор отмены — 200)
    reconcile             сверка: PENDING старше 15 минут -> DECLINED (GATEWAY_TIMEOUT)
    qr       req.json     выпуск QR на чек: nonce, срок 24 ч, подпись, платёжная ссылка (FR-1)
    report   --merchant|--employee --from --to [--daily]   отчёты по payments (ADR-007)

Параметры: --now ISO-время UTC, --qr-file путь к qr_links.csv, --out-dir каталог для *_out.csv.
Секрет подписи берётся только из переменной окружения TIPS_HMAC_SECRET.

Коды выхода: 0 — операция успешна (SUCCESS, CREATED, CANCELED), 1 — платёж не SUCCESS
(DECLINED или PENDING), 2 — ошибка проверки или запроса.

Решения: docs/adr-001 (эмулятор), 002 (статусы), 003 (подпись и сумма чека),
004 (порядок проверок, Decimal), 005 (колонки), 008 (идемпотентность), 009 (секрет, время).
"""
import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "01_данные")
SCENARIOS_CSV = os.path.join(BASE_DIR, "..", "data", "gateway_scenarios.csv")
PAY_URL = "https://tips.example/pay"
QR_TTL = timedelta(hours=24)

SEED_FIELDS = ["payment_id", "merchant_id", "employee_id", "amount", "tip", "status", "timestamp"]
OUT_FIELDS = SEED_FIELDS + ["qr_id", "idempotency_key", "amount_total", "error_code"]
QR_FIELDS = ["qr_id", "merchant_id", "employee_id", "receipt_id", "nonce", "exp", "signature"]
RECEIPT_FIELDS = ["merchant_id", "receipt_id", "amount", "currency"]
AUDIT_FIELDS = ["event_id", "payment_id", "qr_id", "action", "from_status", "to_status",
                "amount_total", "actor", "at"]

TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
CENT = Decimal("0.01")
MAX_BASE = Decimal("1000000")
MAX_TOTAL = Decimal("2000000")
MAX_TIP_SHARE = Decimal("0.5")
RECONCILE_AFTER = timedelta(minutes=15)

# Ошибка -> (HTTP-аналог из ADR-006, код выхода процесса)
ERRORS = {
    "VALIDATION_ERROR": (400, False, "Неверный формат запроса"),
    "INVALID_QR": (422, False, "Не удалось открыть оплату"),
    "QR_EXPIRED": (410, True, "QR устарел, попросите официанта новый"),
    "QR_USED": (409, False, "Этот чек уже оплачен"),
    "MERCHANT_BLOCKED": (403, False, "Оплата в заведении недоступна"),
    "RECEIPT_MISMATCH": (422, False, "Не удалось открыть оплату"),
    "AMOUNT_MISMATCH": (422, False, "Сумма чека изменилась, обновите страницу"),
    "AMOUNT_OUT_OF_RANGE": (422, False, "Некорректная сумма"),
    "TIP_OUT_OF_RANGE": (422, True, "Чаевые — не больше 50 % от чека"),
    "IDEMPOTENCY_CONFLICT": (409, False, "Обновите страницу"),
    "PAYMENT_IN_PROGRESS": (409, True, "Оплата уже обрабатывается"),
    "CANCEL_NOT_ALLOWED": (409, False, "Отмена невозможна: оплата уже отправлена в банк или закрыта"),
    "PAYMENT_STATE_CONFLICT": (409, False, "Оплата была отменена"),
    "NOT_FOUND": (404, False, "Не удалось открыть оплату"),
    "ACCESS_DENIED": (403, False, "Нет доступа"),
}  # код -> (HTTP-аналог, retryable, текст для клиента); совпадает с docs/adr-006 и api/05_api_schemas.yaml


class PaymentError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------- секрет, время, чтение данных ----------

def get_secret():
    """ADR-009: без значения по умолчанию, нет переменной — нет запуска."""
    value = os.environ.get("TIPS_HMAC_SECRET")
    if not value:
        sys.exit("Ошибка: не задана переменная окружения TIPS_HMAC_SECRET")
    return value.encode()


def parse_time(text):
    return datetime.strptime(text, TIME_FMT).replace(tzinfo=timezone.utc)


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_qr(qr_file, qr_id):
    for row in read_csv(qr_file):
        if row["qr_id"] == qr_id:
            return row
    return None


def load_merchant_status(merchant_id):
    for row in read_csv(os.path.join(DATA_DIR, "merchants.csv")):
        if row["merchant_id"] == merchant_id:
            return row["status"]
    return None


def load_employee(employee_id):
    for row in read_csv(os.path.join(DATA_DIR, "employees.csv")):
        if row["employee_id"] == employee_id:
            return row
    return None


def find_qr(qr_file, store, qr_id):
    """QR из qr_file (seed), затем выпущенные прототипом (qr_out.csv)."""
    return load_qr(qr_file, qr_id) or next((r for r in store.qrs() if r["qr_id"] == qr_id), None)


def receipt_amount(store, merchant_id, receipt_id):
    """ADR-003: сумма чека из реестра: сначала чеки, добавленные при выпуске QR, затем seed."""
    for row in store.receipts():
        if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id:
            return Decimal(row["amount"])
    for row in read_csv(os.path.join(DATA_DIR, "qr_links.csv")):
        if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id:
            return load_receipt_amount(row["qr_id"])
    return None


def load_receipt_amount(qr_id):
    """ADR-003: реестр чеков прототипа — payments.csv, p_000N <-> qr_00N, amount = сумма чека."""
    payment_id = "p_" + qr_id[len("qr_"):].zfill(4)
    for row in read_csv(os.path.join(DATA_DIR, "payments.csv")):
        if row["payment_id"] == payment_id:
            return Decimal(row["amount"])
    return None


def load_scenarios():
    """ADR-001: явные тестовые исходы, как тестовые карты в sandbox шлюза."""
    return {r["qr_id"]: (r["result"], r["error_code"] or None) for r in read_csv(SCENARIOS_CSV)}


# ---------- проверки QR ----------

def compute_signature(secret, qr):
    payload = "|".join([qr["merchant_id"], qr["employee_id"], qr["receipt_id"], qr["nonce"], qr["exp"]])
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:16]


def verify_signature(secret, qr, sig):
    """Q-1: подпись записи и подпись из ссылки должны совпасть с HMAC (ADR-003)."""
    expected = compute_signature(secret, qr)
    return (hmac.compare_digest(expected, qr["signature"])
            and isinstance(sig, str) and hmac.compare_digest(expected, sig))


def check_exp(qr, now):
    """Q-2: QR действует, пока exp >= now."""
    return parse_time(qr["exp"]) >= now


# ---------- деньги ----------

def to_money(value):
    """Сумма не больше чем с двумя знаками после запятой: лишние знаки — ошибка, а не тихое округление."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.quantize(CENT):
            raise InvalidOperation
        return amount.quantize(CENT)
    except InvalidOperation:
        raise PaymentError("VALIDATION_ERROR")


def calc_amounts(base, tip_pct=None, tip_amount=None):
    """ADR-004: Decimal + ROUND_HALF_UP, float для денег не используется."""
    if tip_amount is None:
        tip_amount = (base * Decimal(tip_pct) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return tip_amount, base + tip_amount


def fmt(value):
    return f"{value:.2f}"


# ---------- эмулятор шлюза ----------

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


# ---------- хранилище: payments_out.csv и audit_out.csv ----------

class Store:
    def __init__(self, out_dir):
        self.payments_path = os.path.join(out_dir, "payments_out.csv")
        self.audit_path = os.path.join(out_dir, "audit_out.csv")
        self.qr_path = os.path.join(out_dir, "qr_out.csv")
        self.receipts_path = os.path.join(out_dir, "receipts_out.csv")
        self.png_dir = os.path.join(out_dir, "qr_png")

    def qrs(self):
        return read_csv(self.qr_path) if os.path.exists(self.qr_path) else []

    def receipts(self):
        return read_csv(self.receipts_path) if os.path.exists(self.receipts_path) else []

    @staticmethod
    def append_row(path, fields, row):
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new_file:
                w.writeheader()
            w.writerow(row)

    def payments(self):
        return read_csv(self.payments_path) if os.path.exists(self.payments_path) else []

    def audit(self):
        return read_csv(self.audit_path) if os.path.exists(self.audit_path) else []

    def next_payment_id(self):
        """ADR-008: последовательно, после seed (p_0100) — с p_0101."""
        last = 100
        for row in self.payments():
            last = max(last, int(row["payment_id"][2:]))
        return f"p_{last + 1:04d}"

    def save_payments(self, rows):
        """Перезапись через временный файл: файл не остаётся наполовину записанным."""
        tmp = self.payments_path + ".part"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, self.payments_path)

    def add_audit(self, payment, action, from_status, actor, now):
        rows = self.audit()
        new_file = not os.path.exists(self.audit_path)
        with open(self.audit_path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({
                "event_id": f"evt_{len(rows) + 1:04d}",
                "payment_id": payment["payment_id"],
                "qr_id": payment["qr_id"],
                "action": action,
                "from_status": from_status or "",
                "to_status": payment["status"],
                "amount_total": payment["amount_total"],
                "actor": actor,
                "at": now.strftime(TIME_FMT),
            })

    def transition(self, payment, new_status, error, action, actor, now):
        """Смена статуса и аудит — вместе (шаг 8 ADR-004)."""
        old = payment["status"]
        payment["status"] = new_status
        payment["error_code"] = error or ""
        rows = [payment if r["payment_id"] == payment["payment_id"] else r for r in self.payments()]
        self.save_payments(rows)
        self.add_audit(payment, action, old, actor, now)


# ---------- операции ----------

def validate_request(req):
    """Шаг 1: формат запроса. tip_pct и tip_amount — ровно одно из двух."""
    if not isinstance(req, dict):
        raise PaymentError("VALIDATION_ERROR")
    if not isinstance(req.get("qr_id"), str) or not re.fullmatch(r"qr_\d{3,}", req["qr_id"]):
        raise PaymentError("VALIDATION_ERROR")
    if not isinstance(req.get("receipt_id"), str) or not re.fullmatch(r"r_\d{4}", req["receipt_id"]):
        raise PaymentError("VALIDATION_ERROR")
    if "base_amount" not in req or isinstance(req["base_amount"], bool):
        raise PaymentError("VALIDATION_ERROR")
    if ("tip_pct" in req) == ("tip_amount" in req):
        raise PaymentError("VALIDATION_ERROR")
    key = req.get("idempotency_key")
    if key is None:
        key = str(uuid.uuid4())  # CLI-удобство: клиент в реальной системе шлёт ключ сам
    if not isinstance(key, str) or not key:
        raise PaymentError("VALIDATION_ERROR")
    return key


def create_payment(req, secret, qr_file, store, now):
    key = validate_request(req)
    qr_id = req["qr_id"]
    base = to_money(req["base_amount"])
    tip_pct = req.get("tip_pct")
    tip_in = to_money(req["tip_amount"]) if "tip_amount" in req else None
    if tip_pct is not None and (isinstance(tip_pct, bool) or not isinstance(tip_pct, (int, Decimal))
                                or tip_pct != int(tip_pct)):
        raise PaymentError("VALIDATION_ERROR")

    # Q-1. Неизвестный qr_id и плохая подпись дают один ответ: перебор id ничего не раскрывает.
    qr = find_qr(qr_file, store, qr_id)
    if qr is None or not verify_signature(secret, qr, req.get("sig")):
        raise PaymentError("INVALID_QR")

    # P-2 (ADR-008). Повтор ключа раньше проверок exp/nonce: иначе повтор после SUCCESS
    # получил бы QR_USED вместо уже созданного платежа.
    existing = [p for p in store.payments() if p["qr_id"] == qr_id]
    for p in existing:
        if p["idempotency_key"] == key:
            same = p["amount"] == fmt(base) and req["receipt_id"] == qr["receipt_id"] and (
                tip_in == Decimal(p["tip"]) if tip_in is not None
                else Decimal(p["tip"]) == calc_amounts(base, tip_pct)[0])
            if not same:
                raise PaymentError("IDEMPOTENCY_CONFLICT")
            return p, True

    # Q-2 .. Q-4
    if not check_exp(qr, now):
        raise PaymentError("QR_EXPIRED")
    if any(p["status"] == "SUCCESS" for p in existing):  # nonce гасится на SUCCESS (ADR-002)
        raise PaymentError("QR_USED")
    if load_merchant_status(qr["merchant_id"]) != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    if req["receipt_id"] != qr["receipt_id"]:  # Q-5: чек из запроса должен быть чеком этого QR
        raise PaymentError("RECEIPT_MISMATCH")

    # Сумма чека — серверная правда (ADR-003), затем P-1, Q-6, P-3
    receipt = receipt_amount(store, qr["merchant_id"], qr["receipt_id"])
    if receipt is None:
        raise PaymentError("NOT_FOUND")
    if base != receipt:
        raise PaymentError("AMOUNT_MISMATCH")
    if not (0 < base <= MAX_BASE):
        raise PaymentError("AMOUNT_OUT_OF_RANGE")
    if tip_pct is not None and not (0 <= tip_pct <= 50):
        raise PaymentError("TIP_OUT_OF_RANGE")
    if tip_in is not None and not (0 <= tip_in <= base * MAX_TIP_SHARE):
        raise PaymentError("TIP_OUT_OF_RANGE")
    tip, total = calc_amounts(base, tip_pct, tip_in)
    if total > MAX_TOTAL:
        raise PaymentError("AMOUNT_OUT_OF_RANGE")

    # Блокировка QR, пока есть платёж в CREATED или PENDING (ADR-002)
    if any(p["status"] in ("CREATED", "PENDING") for p in existing):
        raise PaymentError("PAYMENT_IN_PROGRESS")

    payment = {
        "payment_id": store.next_payment_id(),
        "merchant_id": qr["merchant_id"],
        "employee_id": qr["employee_id"],
        "amount": fmt(base),
        "tip": fmt(tip),
        "status": "CREATED",
        "timestamp": now.strftime(TIME_FMT),
        "qr_id": qr_id,
        "idempotency_key": key,
        "amount_total": fmt(total),
        "error_code": "",
    }
    store.save_payments(store.payments() + [payment])
    store.add_audit(payment, "payment_created", None, "client", now)
    return payment, False


def find_payment(store, payment_id):
    for p in store.payments():
        if p["payment_id"] == payment_id:
            return p
    raise PaymentError("NOT_FOUND")


def confirm_payment(payment, qr_file, store, now):
    status = payment["status"]
    if status == "CANCELED":  # подтверждать нечего (ADR-002)
        raise PaymentError("PAYMENT_STATE_CONFLICT")
    if status != "CREATED":  # повтор: текущий статус и прежний код, шлюз не вызывается
        return payment
    qr = find_qr(qr_file, store, payment["qr_id"])
    if qr is None:  # QR выпущен в другом файле: укажите тот же --qr-file, что при создании
        raise PaymentError("NOT_FOUND")
    store.transition(payment, "PENDING", None, "payment_submitted", "client", now)
    result, error = emulate_gateway(payment["qr_id"], qr["nonce"], load_scenarios())
    if result == "SUCCESS":
        store.transition(payment, "SUCCESS", None, "payment_confirmed", "system", now)
    elif result == "DECLINED":
        store.transition(payment, "DECLINED", error, "payment_declined", "system", now)
    else:  # таймаут: остаёмся в PENDING, ждём сверку, перехода нет — аудит не пишем
        payment["error_code"] = error
        store.save_payments([payment if r["payment_id"] == payment["payment_id"] else r
                             for r in store.payments()])
    return payment


def cancel_payment(payment, store, now):
    if payment["status"] == "CANCELED":  # повтор отмены идемпотентен (ADR-002)
        return payment
    if payment["status"] != "CREATED":
        raise PaymentError("CANCEL_NOT_ALLOWED")
    store.transition(payment, "CANCELED", None, "payment_canceled", "client", now)
    return payment


def reconcile(store, now):
    """ADR-001/002: PENDING без ответа 15 минут -> DECLINED (GATEWAY_TIMEOUT)."""
    submitted = {}
    for ev in store.audit():
        if ev["action"] == "payment_submitted":
            submitted[ev["payment_id"]] = parse_time(ev["at"])
    closed = []
    for p in store.payments():
        if p["status"] == "PENDING" and p["payment_id"] in submitted \
                and now - submitted[p["payment_id"]] >= RECONCILE_AFTER:
            store.transition(p, "DECLINED", "GATEWAY_TIMEOUT", "payment_declined", "system", now)
            closed.append(p["payment_id"])
    return closed


# ---------- выпуск QR (FR-1) ----------

def validate_qr_request(req):
    """Формат запроса на выпуск QR; сумма чека приходит от кассы (в прототипе — из запроса)."""
    if not isinstance(req, dict):
        raise PaymentError("VALIDATION_ERROR")
    for key, pattern in (("merchant_id", r"m_\d{3}"), ("employee_id", r"e_\d{3}"), ("receipt_id", r"r_\d{4}")):
        if not isinstance(req.get(key), str) or not re.fullmatch(pattern, req[key]):
            raise PaymentError("VALIDATION_ERROR")
    if "amount" not in req or isinstance(req["amount"], bool):
        raise PaymentError("VALIDATION_ERROR")
    return to_money(req["amount"])


def qr_result(qr, store, new):
    """Платёжная ссылка с подписью (ADR-003) и, если установлен qrencode, PNG с QR-кодом."""
    url = f"{PAY_URL}?qr={qr['qr_id']}&sig={qr['signature']}"
    png = None
    if shutil.which("qrencode"):
        os.makedirs(store.png_dir, exist_ok=True)
        path = os.path.join(store.png_dir, qr["qr_id"] + ".png")
        done = subprocess.run(["qrencode", "-o", path, "-s", "8", url], capture_output=True)
        png = path if done.returncode == 0 else None
    return {"qr_id": qr["qr_id"], "pay_url": url, "exp": qr["exp"], "new": new, "png": png}


def create_qr(req, secret, qr_file, store, now):
    """Выпуск QR на чек. Проверки: мерчант ACTIVE, официант этого мерчанта, чек не меняет сумму."""
    amount = validate_qr_request(req)
    merchant_id, employee_id, receipt_id = req["merchant_id"], req["employee_id"], req["receipt_id"]
    status = load_merchant_status(merchant_id)
    if status is None:
        raise PaymentError("NOT_FOUND")
    if status != "ACTIVE":
        raise PaymentError("MERCHANT_BLOCKED")
    employee = load_employee(employee_id)
    if employee is None:
        raise PaymentError("NOT_FOUND")
    if employee["merchant_id"] != merchant_id:
        raise PaymentError("ACCESS_DENIED")
    if not (0 < amount <= MAX_BASE):
        raise PaymentError("AMOUNT_OUT_OF_RANGE")
    known = receipt_amount(store, merchant_id, receipt_id)
    if known is not None and known != amount:
        raise PaymentError("AMOUNT_MISMATCH")

    rows = read_csv(qr_file) + store.qrs()
    for row in rows:  # на чек уже есть действующий QR — отдаём его, а не выпускаем второй
        if row["merchant_id"] == merchant_id and row["receipt_id"] == receipt_id \
                and parse_time(row["exp"]) >= now:
            if row["employee_id"] != employee_id:
                raise PaymentError("ACCESS_DENIED")
            return qr_result(row, store, new=False)

    last = max((int(r["qr_id"][3:]) for r in rows), default=0)
    qr = {"qr_id": f"qr_{last + 1:03d}", "merchant_id": merchant_id, "employee_id": employee_id,
          "receipt_id": receipt_id, "nonce": secrets.token_hex(3),
          "exp": (now + QR_TTL).strftime(TIME_FMT)}
    qr["signature"] = compute_signature(secret, qr)
    if known is None:  # касса передала новый чек
        store.append_row(store.receipts_path, RECEIPT_FIELDS,
                         {"merchant_id": merchant_id, "receipt_id": receipt_id, "amount": fmt(amount), "currency": "RUB"})
    store.append_row(store.qr_path, QR_FIELDS, qr)
    return qr_result(qr, store, new=True)


# ---------- отчёты (ADR-007) ----------

def parse_day(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise PaymentError("VALIDATION_ERROR")


def round_money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def report_metrics(rows):
    """Чаевые и средние — только по SUCCESS, количество попыток и доля успеха — по всем."""
    success = [r for r in rows if r["status"] == "SUCCESS"]
    tips = sum((Decimal(r["tip"]) for r in success), Decimal(0))
    percent = sum((Decimal(r["tip"]) / Decimal(r["amount"]) * 100 for r in success), Decimal(0))
    by_day = {}
    for r in success:
        by_day[r["timestamp"][:10]] = by_day.get(r["timestamp"][:10], Decimal(0)) + Decimal(r["tip"])
    best = max(by_day.values()) if by_day else None
    return {
        "tips_total": round_money(tips),
        "payments_count": len(rows),
        "avg_tip_pct": round_money(percent / len(success)) if success else None,
        "avg_tip_amount": round_money(tips / len(success)) if success else None,
        "success_rate": round_money(Decimal(len(success)) / len(rows)) if rows else 0.0,
        "top_day": min(d for d in by_day if by_day[d] == best) if by_day else None,
    }


def build_report(store, merchant_id, employee_id, from_text, to_text, daily):
    """Отчёт по мерчанту, официанту или по дням. Payment = seed payments.csv + payments_out.csv."""
    if bool(merchant_id) == bool(employee_id) or (daily and not merchant_id):
        raise PaymentError("VALIDATION_ERROR")
    first, last = parse_day(from_text), parse_day(to_text)
    if first > last:
        raise PaymentError("VALIDATION_ERROR")
    if merchant_id and load_merchant_status(merchant_id) is None:
        raise PaymentError("NOT_FOUND")
    if employee_id and not load_employee(employee_id):
        raise PaymentError("NOT_FOUND")
    key, owner = ("merchant_id", merchant_id) if merchant_id else ("employee_id", employee_id)
    rows = [r for r in read_csv(os.path.join(DATA_DIR, "payments.csv")) + store.payments()
            if r[key] == owner and first.isoformat() <= r["timestamp"][:10] <= last.isoformat()]
    if daily:
        days = sorted({r["timestamp"][:10] for r in rows})
        items = []
        for day in days:
            m = report_metrics([r for r in rows if r["timestamp"][:10] == day])
            items.append({"date": day, "tips_total": m["tips_total"],
                          "payments_count": m["payments_count"], "avg_tip_pct": m["avg_tip_pct"]})
        return {"merchant_id": merchant_id, "daily": items}
    period = first.strftime("%Y-%m")
    if period != last.strftime("%Y-%m"):  # формат period для произвольного интервала в API не определён
        raise PaymentError("VALIDATION_ERROR")
    m = report_metrics(rows)
    if merchant_id:
        return {"merchant_id": merchant_id, "period": period, **{k: m[k] for k in
                ("tips_total", "payments_count", "avg_tip_pct", "success_rate")}}
    return {"employee_id": employee_id, "period": period, **{k: m[k] for k in
            ("tips_total", "payments_count", "avg_tip_amount", "top_day")}}


# ---------- CLI ----------

def money_json(value):
    return float(Decimal(value))


def payment_json(p):
    return {
        "payment_id": p["payment_id"],
        "qr_id": p["qr_id"],
        "status": p["status"],
        "base_amount": money_json(p["amount"]),
        "tip_amount": money_json(p["tip"]),
        "amount_total": money_json(p["amount_total"]),
        "currency": "RUB",
        "error_code": p["error_code"] or None,
        "created_at": p["timestamp"],
    }


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def exit_code(status):
    return 1 if status in ("DECLINED", "PENDING") else 0


def main():
    parser = argparse.ArgumentParser(description="Прототип платформы чаевых (кейс 3)")
    parser.add_argument("--now", help="текущее время UTC, например 2025-11-01T18:00:00Z")
    parser.add_argument("--qr-file", default=os.path.join(DATA_DIR, "qr_links.csv"))
    parser.add_argument("--out-dir", default=BASE_DIR)
    parser.add_argument("--merchant", help="report: merchant_id, например m_001")
    parser.add_argument("--employee", help="report: employee_id, например e_001")
    parser.add_argument("--from", dest="from_date", help="report: первый день YYYY-MM-DD (включительно)")
    parser.add_argument("--to", dest="to_date", help="report: последний день YYYY-MM-DD (включительно)")
    parser.add_argument("--daily", action="store_true", help="report: разбивка по дням для --merchant")
    parser.add_argument("args", nargs="*", help="[pay|create|confirm|cancel|reconcile|qr|report] аргумент")
    ns = parser.parse_args()

    commands = ("pay", "create", "confirm", "cancel", "reconcile", "qr", "report")
    if not ns.args:
        parser.error("нужна команда")
    cmd, rest = (ns.args[0], ns.args[1:]) if ns.args[0] in commands else ("pay", ns.args)
    secret = get_secret() if cmd in ("pay", "create", "qr") else None
    store = Store(ns.out_dir)

    try:
        try:
            now = parse_time(ns.now) if ns.now else datetime.now(timezone.utc).replace(microsecond=0)
        except ValueError:  # --now не в формате 2025-11-01T18:00:00Z
            raise PaymentError("VALIDATION_ERROR")
        if cmd == "reconcile":
            emit({"closed": reconcile(store, now)})
            sys.exit(0)
        if cmd == "report":
            emit(build_report(store, ns.merchant, ns.employee, ns.from_date, ns.to_date, ns.daily))
            sys.exit(0)
        if len(rest) != 1:
            parser.error(f"команде {cmd} нужен один аргумент")
        if cmd in ("pay", "create", "qr"):
            try:
                with open(rest[0], encoding="utf-8") as f:
                    req = json.load(f, parse_float=Decimal)
            except (OSError, ValueError):
                raise PaymentError("VALIDATION_ERROR")
            if cmd == "qr":
                emit(create_qr(req, secret, ns.qr_file, store, now))
                sys.exit(0)
            payment, replay = create_payment(req, secret, ns.qr_file, store, now)
            if cmd == "pay" and not replay:
                payment = confirm_payment(payment, ns.qr_file, store, now)
        elif cmd == "confirm":
            payment = confirm_payment(find_payment(store, rest[0]), ns.qr_file, store, now)
        else:
            payment = cancel_payment(find_payment(store, rest[0]), store, now)
    except PaymentError as e:
        http, retryable, message = ERRORS[e.code]
        emit({"status": "ERROR", "error_code": e.code, "message": message, "retryable": retryable, "http": http})
        sys.exit(2)

    emit(payment_json(payment))
    sys.exit(exit_code(payment["status"]))


if __name__ == "__main__":
    main()
