#!/usr/bin/env python3
"""
Прогон тестов T3-1…T3-9 одной командой (колонка «Факт» для docs/03-tests.md).

    export TIPS_HMAC_SECRET=<демо-ключ из README>
    python3 prototype/run_tests.py

Каждый тест работает в новом временном каталоге, поэтому payments_out.csv и audit_out.csv
в репозитории не трогаются и между тестами не накапливаются.
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(BASE_DIR, "04_прототип.py")
TEST_QR = os.path.join(BASE_DIR, "test_qr_links.csv")
NOW = "2025-11-01T18:00:00Z"


class Env:
    """Один изолированный прогон: свой каталог вывода."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def run(self, *args, now=NOW, qr_file=None):
        cmd = [sys.executable, "-I", PROTO, "--now", now, "--out-dir", self.dir]
        if qr_file:
            cmd += ["--qr-file", qr_file]
        proc = subprocess.run(cmd + list(args), capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else {}

    def req(self, **body):
        path = os.path.join(self.dir, f"req_{len(os.listdir(self.dir))}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f)
        return path

    def rows(self):
        path = os.path.join(self.dir, "payments_out.csv")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))


def sig_of(qr_id):
    with open(os.path.join(BASE_DIR, "01_данные", "qr_links.csv"), encoding="utf-8") as f:
        return next(r["signature"] for r in csv.DictReader(f) if r["qr_id"] == qr_id)


def t1():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5,
                            idempotency_key="k-1"))
    ok = (code == 0 and out["status"] == "SUCCESS" and out["tip_amount"] == 31.85
          and out["amount_total"] == 668.85)
    return ok, f"код {code}, {out['status']}, tip {out['tip_amount']}, total {out['amount_total']}"


def t2():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_003", sig=sig_of("qr_003"), base_amount=911.00, tip_pct=15,
                            idempotency_key="k-2"))
    ok = (code == 1 and out["status"] == "DECLINED" and out["error_code"] == "CARD_DECLINED"
          and out["tip_amount"] == 136.65 and out["amount_total"] == 1047.65)
    return ok, f"код {code}, {out['status']}, {out['error_code']}, tip {out['tip_amount']}, total {out['amount_total']}"


def t3():
    e = Env()
    bad = sig_of("qr_001")[:-1] + ("0" if sig_of("qr_001")[-1] != "0" else "1")
    code, out = e.run(e.req(qr_id="qr_001", sig=bad, base_amount=637.00, tip_pct=5, idempotency_key="k-3"))
    ok = code == 2 and out["error_code"] == "INVALID_QR" and e.rows() == []
    return ok, f"код {code}, {out['error_code']}, строк в payments_out.csv: {len(e.rows())}"


def t3b():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5,
                            idempotency_key="k-3b"), now="2025-11-02T00:00:00Z")
    ok = code == 2 and out["error_code"] == "QR_EXPIRED" and e.rows() == []
    return ok, f"код {code}, {out['error_code']}, строк: {len(e.rows())}"


def t4():
    e = Env()
    req = e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5, idempotency_key="k-4")
    _, first = e.run(req)
    _, second = e.run(req)
    ok = first["payment_id"] == second["payment_id"] == "p_0101" and len(e.rows()) == 1
    return ok, f"{first['payment_id']} и {second['payment_id']}, строк: {len(e.rows())}"


def t5():
    e = Env()
    _, created = e.run("create", e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00,
                                       tip_pct=5, idempotency_key="k-5"))
    code1, canceled = e.run("cancel", created["payment_id"])
    e2 = Env()
    e2.run(e2.req(qr_id="qr_010", sig=sig_of("qr_010"), base_amount=e2_amount("qr_010"), tip_pct=10,
                  idempotency_key="k-5b"))
    code2, out2 = e2.run("cancel", "p_0101")
    ok = (created["status"] == "CREATED" and code1 == 0 and canceled["status"] == "CANCELED"
          and code2 == 2 and out2["error_code"] == "CANCEL_NOT_ALLOWED")
    return ok, (f"create {created['status']} -> cancel {canceled['status']}; "
                f"отмена из PENDING: {out2['error_code']}")


def e2_amount(qr_id):
    pid = "p_" + qr_id[3:].zfill(4)
    with open(os.path.join(BASE_DIR, "01_данные", "payments.csv"), encoding="utf-8") as f:
        return next(float(r["amount"]) for r in csv.DictReader(f) if r["payment_id"] == pid)


def t6():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_010", sig=sig_of("qr_010"), base_amount=e2_amount("qr_010"),
                            tip_pct=10, idempotency_key="k-6"))
    ok = code == 1 and out["status"] == "PENDING" and out["error_code"] == "GATEWAY_TIMEOUT"
    return ok, f"код {code}, {out['status']}, {out['error_code']}"


def t7():
    e = Env()
    with open(TEST_QR, encoding="utf-8") as f:
        sig = next(csv.DictReader(f))["signature"]
    code, out = e.run(e.req(qr_id="qr_101", sig=sig, base_amount=500.00, tip_pct=10, idempotency_key="k-7"),
                      qr_file=TEST_QR)
    ok = code == 2 and out["error_code"] == "MERCHANT_BLOCKED" and e.rows() == []
    return ok, f"код {code}, {out['error_code']}, строк: {len(e.rows())}"


def t8():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=51,
                            idempotency_key="k-8"))
    ok = code == 2 and out["error_code"] == "TIP_OUT_OF_RANGE" and e.rows() == []
    return ok, f"код {code}, {out['error_code']}"


def t9():
    e = Env()
    e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5, idempotency_key="k-9a"))
    code, out = e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5,
                            idempotency_key="k-9b"))
    ok = code == 2 and out["error_code"] == "QR_USED" and len(e.rows()) == 1
    return ok, f"код {code}, {out['error_code']}, строк: {len(e.rows())}"


TESTS = [("T3-1 Успех", t1), ("T3-2 Отказ", t2), ("T3-3 Неверный QR", t3), ("T3-3б QR истёк", t3b),
         ("T3-4 Повтор", t4), ("T3-5 Отмена", t5), ("T3-6 Таймаут", t6), ("T3-7 Блокировка", t7),
         ("T3-8 Чаевые > 50 %", t8), ("T3-9 Повторная оплата", t9)]


def main():
    if not os.environ.get("TIPS_HMAC_SECRET"):
        sys.exit("Задайте TIPS_HMAC_SECRET (демо-ключ организаторов, см. README)")
    failed = 0
    for name, fn in TESTS:
        ok, fact = fn()
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {fact}")
    print(f"\nИтого: {len(TESTS) - failed} из {len(TESTS)} пройдено")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
