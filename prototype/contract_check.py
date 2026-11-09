#!/usr/bin/env python3
"""
Сверка спецификации и реализации: ответы консольного прототипа проверяются по схемам api/05_api_schemas.yaml.

    pip install pyyaml jsonschema
    python3 prototype/contract_check.py          # код выхода 1, если реализация расходится с контрактом

Прототип запускается как настоящий клиент (через командную строку, во временном каталоге).
Проверяется три вещи:
  1. запросы, которые мы шлём, проходят схемы QRCreate и TipCreate;
  2. ответы (платёж, QR, три вида отчёта, ошибка) проходят схемы Payment, QRCreated, Report,
     EmployeeReport, DailyReport, Error;
  3. HTTP-код каждой ошибки описан в спецификации для этой операции, а коды каталога ошибок
     прототипа входят в enum схемы Error.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid

import jsonschema
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(BASE_DIR, "..", "api", "05_api_schemas.yaml")
PROTO = os.path.join(BASE_DIR, "04_прототип.py")
NOW = "2025-11-01T18:00:00Z"


def load_spec():
    with open(SPEC, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(node, spec):
    """Подставляет $ref и приводит OpenAPI 3.0 (nullable, булев exclusiveMinimum) к виду, понятному валидатору."""
    if isinstance(node, list):
        return [resolve(x, spec) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return resolve(target, spec)
    out = {k: resolve(v, spec) for k, v in node.items() if k not in ("example", "nullable")}
    if node.get("nullable"):
        out = {"anyOf": [out, {"type": "null"}]}
    return out


class Contract:
    def __init__(self):
        self.spec = load_spec()
        self.failures = []
        self.checked = 0

    def schema(self, name):
        return resolve({"$ref": f"#/components/schemas/{name}"}, self.spec)

    def check(self, label, name, body):
        """Тело body должно проходить схему name."""
        self.checked += 1
        validator = jsonschema.Draft4Validator(self.schema(name))
        for err in sorted(validator.iter_errors(body), key=lambda e: list(e.path)):
            where = ".".join(str(p) for p in err.path) or "(корень)"
            self.failures.append(f"{label}: схема {name}, поле {where}: {err.message}")

    def documented(self, path, method):
        """HTTP-коды, описанные в спецификации для операции."""
        return {int(c) for c in resolve(self.spec["paths"][path][method]["responses"], self.spec)}

    def check_http(self, label, path, method, out):
        self.checked += 1
        code = out.get("http")
        if code not in self.documented(path, method):
            self.failures.append(f"{label}: {method.upper()} {path} не описывает код {code} ({out.get('error_code')})")


class Run:
    """Изолированный каталог вывода, как в тестах."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.n = 0

    def req(self, body):
        self.n += 1
        path = os.path.join(self.dir, f"req_{self.n}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f)
        return path

    def cli(self, *args):
        cmd = [sys.executable, "-I", PROTO, "--now", NOW, "--out-dir", self.dir] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}


def sig_of(qr_id):
    import csv
    with open(os.path.join(BASE_DIR, "01_данные", "qr_links.csv"), encoding="utf-8") as f:
        return next(r["signature"] for r in csv.DictReader(f) if r["qr_id"] == qr_id)


def tip(qr_id, base, **extra):
    return dict(qr_id=qr_id, receipt_id="r_%d" % (1000 + int(qr_id[3:])), sig=sig_of(qr_id), base_amount=base,
                idempotency_key=str(uuid.uuid4()), **extra)


def api_request(body):
    """То, что уходит в API: подпись из ссылки QR входит в тело TipCreate (контракт v1.1)."""
    return dict(body)


def pay_amount(qr_id):
    import csv
    with open(os.path.join(BASE_DIR, "01_данные", "payments.csv"), encoding="utf-8") as f:
        return float(next(r["amount"] for r in csv.DictReader(f) if r["payment_id"] == "p_" + qr_id[3:].zfill(4)))


def run_checks():
    c = Contract()

    # --- QR: выпуск на чек
    r = Run()
    qr_body = dict(merchant_id="m_001", employee_id="e_001", receipt_id="r_2001")
    c.check("qr: запрос", "QRCreate", qr_body)
    out = r.cli("qr", r.req(dict(qr_body, amount=1250.50)))
    c.check("qr: ответ", "QRCreated", out)

    # --- платежи: успех, отказ, создание/подтверждение/отмена
    r = Run()
    body = tip("qr_001", 637.00, tip_pct=5)
    c.check("pay SUCCESS: запрос", "TipCreate", api_request(body))
    c.check("pay SUCCESS: ответ", "Payment", r.cli("pay", r.req(body)))

    r = Run()
    body = tip("qr_003", 911.00, tip_pct=15)
    c.check("pay DECLINED: ответ", "Payment", r.cli("pay", r.req(body)))

    r = Run()
    body = tip("qr_002", pay_amount("qr_002"), tip_amount=10.50)
    c.check("create (tip_amount): запрос", "TipCreate", api_request(body))
    created = r.cli("create", r.req(body))
    c.check("create: ответ", "Payment", created)
    c.check("confirm: ответ", "Payment", r.cli("confirm", created["payment_id"]))

    r = Run()
    created = r.cli("create", r.req(tip("qr_004", pay_amount("qr_004"), tip_pct=10)))
    c.check("cancel: ответ", "Payment", r.cli("cancel", created["payment_id"]))

    r = Run()  # QR из таблицы сценариев уходит в PENDING по таймауту
    c.check("pay PENDING: ответ", "Payment", r.cli("pay", r.req(tip("qr_010", pay_amount("qr_010"), tip_pct=10))))

    # --- ошибки: форма Error и код, описанный в спецификации
    r = Run()
    bad = tip("qr_001", 637.00, tip_pct=5)
    bad["sig"] = "0" * 16
    out = r.cli("pay", r.req(bad))
    c.check("INVALID_QR", "Error", out)
    c.check_http("INVALID_QR", "/api/v1/tips", "post", out)

    out = r.cli("pay", r.req(tip("qr_001", 637.00, tip_pct=60)))
    c.check("TIP_OUT_OF_RANGE", "Error", out)
    c.check_http("TIP_OUT_OF_RANGE", "/api/v1/tips", "post", out)

    out = r.cli("pay", r.req(tip("qr_001", 100.00, tip_pct=5)))
    c.check("AMOUNT_MISMATCH", "Error", out)
    c.check_http("AMOUNT_MISMATCH", "/api/v1/tips", "post", out)

    out = r.cli("pay", r.req(tip("qr_001", 637.005, tip_pct=5)))  # три знака после запятой
    c.check("VALIDATION_ERROR", "Error", out)
    c.check_http("VALIDATION_ERROR", "/api/v1/tips", "post", out)

    out = r.cli("confirm", "p_9999")
    c.check("confirm NOT_FOUND", "Error", out)
    c.check_http("confirm NOT_FOUND", "/api/v1/payments/{id}/confirm", "post", out)

    r = Run()
    first = r.cli("pay", r.req(tip("qr_001", 637.00, tip_pct=5)))
    out = r.cli("cancel", first["payment_id"])
    c.check("cancel CANCEL_NOT_ALLOWED", "Error", out)
    c.check_http("cancel CANCEL_NOT_ALLOWED", "/api/v1/payments/{id}/cancel", "post", out)

    r = Run()
    body = tip("qr_002", pay_amount("qr_002"), tip_pct=5)
    r.cli("pay", r.req(body))
    out = r.cli("pay", r.req(dict(body, tip_pct=10)))
    c.check("IDEMPOTENCY_CONFLICT", "Error", out)
    c.check_http("IDEMPOTENCY_CONFLICT", "/api/v1/tips", "post", out)

    out = r.cli("qr", r.req(dict(merchant_id="m_019", employee_id="e_055", receipt_id="r_2001", amount=1250.50)))
    c.check("MERCHANT_BLOCKED", "Error", out)
    c.check_http("MERCHANT_BLOCKED", "/api/v1/qr", "post", out)

    # --- отчёты
    r = Run()
    period = ("--from", "2025-11-01", "--to", "2025-11-30")
    c.check("report: мерчант", "Report", r.cli("report", "--merchant", "m_001", *period))
    c.check("report: официант", "EmployeeReport", r.cli("report", "--employee", "e_001", *period))
    c.check("report: по дням", "DailyReport", r.cli("report", "--merchant", "m_001", "--daily", *period))

    # --- каталог ошибок прототипа и enum спецификации
    spec = importlib.util.spec_from_file_location("proto", PROTO)
    proto = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proto)
    enum = set(c.schema("Error")["properties"]["error_code"]["enum"])
    all_codes = {code for resp in c.spec["paths"].values() for op in resp.values() for code in op.get("responses", {})}
    for code, (http, _retryable, _message) in proto.ERRORS.items():
        c.checked += 1
        if code not in enum:
            c.failures.append(f"каталог ошибок: {code} нет в enum схемы Error")
        if str(http) not in all_codes:
            c.failures.append(f"каталог ошибок: HTTP {http} ({code}) не описан ни в одной операции")
    return c


def main():
    c = run_checks()
    for line in c.failures:
        print("РАСХОЖДЕНИЕ", line)
    print(f"Проверок: {c.checked}, расхождений: {len(c.failures)}")
    sys.exit(1 if c.failures else 0)


if __name__ == "__main__":
    main()
