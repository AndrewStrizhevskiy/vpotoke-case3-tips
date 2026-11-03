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
        if "qr_id" in body:  # запрос платежа: чек по умолчанию — чек этого QR (qr_001 -> r_1001)
            body.setdefault("receipt_id", "r_%d" % (1000 + int(body["qr_id"][3:])))
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


def t3c():
    e = Env()
    code, out = e.run(e.req(qr_id="qr_004", sig=sig_of("qr_004"), receipt_id="r_9999",
                            base_amount=e2_amount("qr_004"), tip_pct=10, idempotency_key="k-3c"))
    ok = code == 2 and out["error_code"] == "RECEIPT_MISMATCH" and e.rows() == []
    return ok, f"код {code}, {out['error_code']}, строк: {len(e.rows())}"


def t4():
    e = Env()
    req = e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5, idempotency_key="k-4")
    _, first = e.run(req)
    _, second = e.run(req)
    ok = first["payment_id"] == second["payment_id"] == "p_0101" and len(e.rows()) == 1
    return ok, f"{first['payment_id']} и {second['payment_id']}, строк: {len(e.rows())}"


def t4b():
    e = Env()
    req = e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5, idempotency_key="k-4b")
    e.run(req)
    code, again = e.run(req)
    ok = code == 0 and again["status"] == "SUCCESS" and again["payment_id"] == "p_0101" and len(e.rows()) == 1
    return ok, f"код {code}, {again.get('status')}, {again.get('payment_id')}, строк: {len(e.rows())}"


def t5b():
    e = Env()
    e.run(e.req(qr_id="qr_001", sig=sig_of("qr_001"), base_amount=637.00, tip_pct=5, idempotency_key="k-5b1"))
    events = len(open(os.path.join(e.dir, "audit_out.csv"), encoding="utf-8").readlines())
    code1, c1 = e.run("confirm", "p_0101")  # повторный confirm после SUCCESS
    events_after = len(open(os.path.join(e.dir, "audit_out.csv"), encoding="utf-8").readlines())
    e.run("create", e.req(qr_id="qr_003", sig=sig_of("qr_003"), base_amount=911.00, tip_pct=15,
                          idempotency_key="k-5b2"))
    e.run("cancel", "p_0102")
    code2, c2 = e.run("confirm", "p_0102")  # confirm после cancel
    code3, c3 = e.run("cancel", "p_0102")  # повторный cancel
    ok = (code1 == 0 and c1["status"] == "SUCCESS" and events == events_after
          and code2 == 2 and c2["error_code"] == "PAYMENT_STATE_CONFLICT"
          and code3 == 0 and c3["status"] == "CANCELED")
    return ok, (f"confirm после SUCCESS: {c1['status']} без нового аудита; confirm после cancel: "
                f"{c2['error_code']}; повторный cancel: {c3['status']}")


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


def qr_req(e, **over):
    body = dict(merchant_id="m_001", employee_id="e_001", receipt_id="r_2001", amount=1250.50)
    body.update(over)
    return e.req(**body)


def sig_from(url):
    return url.split("sig=")[1]


def t10():
    """FR-1: QR выпущен -> по его ссылке создаётся платёж; повторный выпуск на тот же чек возвращает тот же QR."""
    e = Env()
    code, qr = e.run("qr", qr_req(e))
    again_code, again = e.run("qr", qr_req(e))
    pay = dict(qr_id=qr["qr_id"], sig=sig_from(qr["pay_url"]), receipt_id="r_2001", base_amount=1250.50,
               tip_pct=10, idempotency_key="k-10")
    code2, created = e.run("create", e.req(**pay))
    pay.update(base_amount=1250.00, idempotency_key="k-10b")
    code3, wrong = e.run("create", e.req(**pay))
    ok = (code == 0 and qr["new"] and qr["qr_id"] == "qr_101" and again_code == 0 and not again["new"]
          and again["qr_id"] == qr["qr_id"] and code2 == 0 and created["status"] == "CREATED"
          and created["tip_amount"] == 125.05 and created["amount_total"] == 1375.55
          and code3 == 2 and wrong["error_code"] == "AMOUNT_MISMATCH")
    return ok, (f"{qr['qr_id']} новый, повтор: {again['qr_id']} (new={again['new']}); платёж {created['status']}, "
                f"итог {created['amount_total']}; другая сумма: {wrong['error_code']}")


def t11():
    """Проверки при выпуске QR: заблокированный мерчант, чужой официант, другая сумма чека, формат."""
    e = Env()
    c1, o1 = e.run("qr", qr_req(e, merchant_id="m_019", employee_id="e_055"))
    c2, o2 = e.run("qr", qr_req(e, employee_id="e_004"))
    c3, o3 = e.run("qr", qr_req(e, receipt_id="r_1001", amount=700))
    c4, o4 = e.run("qr", qr_req(e, merchant_id="m1"))
    ok = (c1 == 2 and o1["error_code"] == "MERCHANT_BLOCKED" and c2 == 2 and o2["error_code"] == "ACCESS_DENIED"
          and c3 == 2 and o3["error_code"] == "AMOUNT_MISMATCH" and c4 == 2 and o4["error_code"] == "VALIDATION_ERROR"
          and not os.path.exists(os.path.join(e.dir, "qr_out.csv")))
    return ok, f"{o1['error_code']}, {o2['error_code']}, {o3['error_code']}, {o4['error_code']}; QR не выпущен"


def report(e, *args):
    return e.run("report", *args)


def t12():
    """Отчёты сходятся с payments.csv и ADR-007: мерчант, официант, по дням, пустой период."""
    e = Env()
    _, m = report(e, "--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-11-30")
    _, w = report(e, "--employee", "e_001", "--from", "2025-11-01", "--to", "2025-11-30")
    _, d = report(e, "--merchant", "m_001", "--daily", "--from", "2025-11-01", "--to", "2025-11-30")
    _, z = report(e, "--merchant", "m_001", "--from", "2025-12-01", "--to", "2025-12-31")
    code, bad = report(e, "--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-12-31")
    ok = (m == {"merchant_id": "m_001", "period": "2025-11", "tips_total": 414.85, "payments_count": 6,
                "avg_tip_pct": 9.0, "success_rate": 0.83}
          and w == {"employee_id": "e_001", "period": "2025-11", "tips_total": 83.6, "payments_count": 2,
                    "avg_tip_amount": 41.8, "top_day": "2025-11-03"}
          and d["daily"] == [{"date": "2025-11-01", "tips_total": 245.9, "payments_count": 3, "avg_tip_pct": 10.0},
                             {"date": "2025-11-03", "tips_total": 168.95, "payments_count": 3, "avg_tip_pct": 7.5}]
          and z["tips_total"] == 0.0 and z["payments_count"] == 0 and z["avg_tip_pct"] is None
          and z["success_rate"] == 0.0 and code == 2 and bad["error_code"] == "VALIDATION_ERROR")
    return ok, (f"m_001: {m['tips_total']} / {m['payments_count']} / {m['avg_tip_pct']} / {m['success_rate']}; "
                f"e_001: {w['tips_total']} / {w['payments_count']} / {w['avg_tip_amount']} / {w['top_day']}; "
                f"по дням: {len(d['daily'])}; пустой период: {z['payments_count']}")


TESTS = [("T3-1 Успех", t1), ("T3-2 Отказ", t2), ("T3-3 Неверный QR", t3), ("T3-3б QR истёк", t3b), ("T3-3в Чужой чек", t3c),
         ("T3-4 Повтор", t4), ("T3-4б Повтор после оплаты", t4b), ("T3-5 Отмена", t5), ("T3-5б Повторные действия", t5b), ("T3-6 Таймаут", t6), ("T3-7 Блокировка", t7),
         ("T3-8 Чаевые > 50 %", t8), ("T3-9 Повторная оплата", t9),
         ("T3-10 Выпуск QR", t10), ("T3-11 Проверки выпуска QR", t11), ("T3-12 Отчёты", t12)]


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
