#!/usr/bin/env python3
"""Эталонные выводы CLI: запись (record) и сравнение (check).

    python3 tests/golden_cli.py check tests/golden/cli_baseline.json    # сравнить с эталоном
    python3 tests/golden_cli.py record tests/golden/cli_baseline.json   # перезаписать эталон намеренно

Сценарий из 61 команды гоняется во временном каталоге с фиксированным временем. Случайные nonce, подписи
и UUID маскируются. Любое изменение поведения CLI (в том числе при смене хранилища) видно как расхождение.
Нужна переменная TIPS_HMAC_SECRET? Нет: скрипт сам задаёт демо-ключ организаторов для дочерних процессов.
"""
import csv, json, os, re, subprocess, sys, tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PROTO = os.path.join(REPO, "prototype", "04_прототип.py")
SEED = os.path.join(REPO, "prototype", "01_данные")
NOW = "2025-11-01T18:00:00Z"
rows = lambda f: list(csv.DictReader(open(os.path.join(SEED, f), encoding="utf-8", newline="")))
QR = {r["qr_id"]: r for r in rows("qr_links.csv")}
PAY = {r["payment_id"]: r for r in rows("payments.csv")}
amount = lambda q: float(PAY["p_" + q[3:].zfill(4)]["amount"])
MASK = [(re.compile(r"sig=[0-9a-f]{16}"), "sig=<SIG>"), (re.compile(r'"pay_url": "[^"]+"'), '"pay_url": "<URL>"'),
        (re.compile(r'"png": "[^"]+"'), '"png": "<PNG>"')]
CSV_MASK = [(re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<UUID>"), (re.compile(r"[0-9a-f]{6},(\d{4}-\d{2}-\d{2}T[\d:]+Z),[0-9a-f]{16}"), r"<NONCE>,\1,<SIG>")]

def mask(text, rules):
    for rx, to in rules: text = rx.sub(to, text)
    return text

def run(d, args, now=NOW, secret=True, extra=()):
    env = {k: v for k, v in os.environ.items() if k != "TIPS_HMAC_SECRET"}
    if secret: env["TIPS_HMAC_SECRET"] = "demo-secret-k3"
    cmd = [sys.executable, "-I", PROTO] + (["--now", now] if now else []) + ["--out-dir", d] + list(extra) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return {"cmd": " ".join(a if not a.startswith(d) else os.path.basename(a) for a in cmd[3:]).replace(os.path.basename(d), "<D>").replace(REPO, "<REPO>"),
            "code": p.returncode, "out": mask(p.stdout, MASK), "err": p.stderr.strip()[-300:]}

def main():
    out = []
    d = tempfile.mkdtemp()
    n = [0]
    def req(**b):
        n[0] += 1
        p = os.path.join(d, f"r{n[0]}.json")
        q = b.get("qr_id")
        if q in QR:
            b.setdefault("sig", QR[q]["signature"]); b.setdefault("receipt_id", QR[q]["receipt_id"])
            b.setdefault("base_amount", amount(q))
        elif q:
            b.setdefault("receipt_id", "r_1999"); b.setdefault("base_amount", 100.0)
        json.dump(b, open(p, "w"))
        return p
    def raw(text):
        n[0] += 1; p = os.path.join(d, f"r{n[0]}.json"); open(p, "w").write(text); return p
    R = lambda *a, **k: out.append(run(d, *a, **k))
    R(["pay", req(qr_id="qr_001", tip_pct=5, idempotency_key="k1")])
    R(["pay", req(qr_id="qr_001", tip_pct=5, idempotency_key="k1")])
    R([req(qr_id="qr_001", tip_pct=5, idempotency_key="k1")])
    R(["pay", req(qr_id="qr_001", tip_pct=5, idempotency_key="k1b")])
    R(["pay", req(qr_id="qr_001", tip_pct=7, idempotency_key="k1")])
    R(["pay", req(qr_id="qr_003", tip_pct=15, idempotency_key="k3")])
    R(["pay", req(qr_id="qr_003", tip_pct=15, idempotency_key="k3b")])
    R(["pay", req(qr_id="qr_002", sig="0000000000000000", tip_pct=5, idempotency_key="k4")])
    R(["pay", req(qr_id="qr_002", tip_pct=5, idempotency_key="k5")], now="2025-11-02T00:00:00Z")
    R(["pay", req(qr_id="qr_002", receipt_id="r_9999", tip_pct=5, idempotency_key="k6")])
    R(["pay", req(qr_id="qr_002", tip_pct=51, idempotency_key="k7")])
    R(["pay", req(qr_id="qr_002", tip_amount=1000, idempotency_key="k8")])
    R(["pay", req(qr_id="qr_002", tip_amount=10.505, idempotency_key="k9")])
    R(["pay", req(qr_id="qr_002", base_amount=1.0, tip_pct=5, idempotency_key="k10")])
    R(["pay", req(qr_id="qr_002", tip_pct=5, tip_amount=3, idempotency_key="k11")])
    R(["pay", req(qr_id="qr_002", idempotency_key="k12")])
    R(["pay", req(qr_id="qr_999", sig="abc", tip_pct=5, idempotency_key="k13")])
    R(["pay", raw("{not json")])
    R(["pay", os.path.join(d, "missing.json")])
    R(["pay", req(qr_id="qr_002", tip_pct=5)])
    # зависание и сверка
    R(["pay", req(qr_id="qr_010", tip_pct=10, idempotency_key="k20")])
    R(["cancel", "p_0103"]); R(["confirm", "p_0103"]); R(["reconcile"], now="2025-11-01T18:05:00Z")
    R(["reconcile"], now="2025-11-01T18:15:00Z"); R(["cancel", "p_0103"]); R(["confirm", "p_0103"])
    # раздельные шаги
    R(["create", req(qr_id="qr_005", tip_pct=10, idempotency_key="k30")])
    R(["create", req(qr_id="qr_005", tip_pct=10, idempotency_key="k31")])
    R(["cancel", "p_0104"]); R(["cancel", "p_0104"]); R(["confirm", "p_0104"])
    R(["create", req(qr_id="qr_006", tip_pct=0, idempotency_key="k32")])
    R(["confirm", "p_0105"]); R(["confirm", "p_0105"]); R(["cancel", "p_0105"])
    R(["confirm", "p_9999"]); R(["cancel", "p_9999"])
    # блокировка мерчанта (тестовый QR из репозитория)
    tq = list(csv.DictReader(open(os.path.join(REPO, "prototype", "test_qr_links.csv"), encoding="utf-8")))[0]
    R(["pay", req(qr_id=tq["qr_id"], sig=tq["signature"], receipt_id=tq["receipt_id"], base_amount=500.0, tip_pct=10, idempotency_key="k40")],
      extra=["--qr-file", os.path.join(REPO, "prototype", "test_qr_links.csv")])
    # выпуск QR
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_2001", "amount": 1250.5}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_2001", "amount": 1250.5}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_2001", "amount": 99}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_002", "receipt_id": "r_2001", "amount": 1250.5}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_019", "employee_id": "e_055", "receipt_id": "r_2002", "amount": 100}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_004", "receipt_id": "r_2003", "amount": 100}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "bad", "amount": 100}))])
    R(["qr", raw(json.dumps({"merchant_id": "m_777", "employee_id": "e_001", "receipt_id": "r_2004", "amount": 100}))])
    # отчёты
    for a in (["--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-11-30"],
              ["--employee", "e_001", "--from", "2025-11-01", "--to", "2025-11-30"],
              ["--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-11-30", "--daily"],
              ["--merchant", "m_001", "--from", "2026-01-01", "--to", "2026-01-31"],
              ["--merchant", "m_001", "--employee", "e_001", "--from", "2025-11-01", "--to", "2025-11-30"],
              ["--merchant", "m_001", "--from", "2025-11-30", "--to", "2025-11-01"],
              ["--merchant", "m_001", "--from", "2025-10-15", "--to", "2025-11-30"],
              ["--merchant", "m_777", "--from", "2025-11-01", "--to", "2025-11-30"],
              ["--employee", "e_777", "--from", "2025-11-01", "--to", "2025-11-30"],
              ["--merchant", "m_005", "--from", "2025-11-01", "--to", "2025-11-30", "--daily"]):
        R(["report"] + a)
    # прочее
    R(["pay", req(qr_id="qr_007", tip_pct=5, idempotency_key="k50")], now="2025-11-01")
    R(["pay", req(qr_id="qr_007", tip_pct=5, idempotency_key="k50")], secret=False)
    R([], now=None)
    R(["report", "--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-11-30"], secret=False)
    files = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".csv"):
            files[f] = mask(open(os.path.join(d, f), encoding="utf-8").read(), CSV_MASK)
    return {"runs": out, "files": files}

if __name__ == "__main__":
    mode, path = sys.argv[1], sys.argv[2]
    result = main()
    if mode == "record":
        json.dump(result, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("recorded", len(result["runs"]), "runs,", len(result["files"]), "files")
    else:
        old = json.load(open(path, encoding="utf-8"))
        bad = 0
        for a, b in zip(old["runs"], result["runs"]):
            if a != b:
                bad += 1; print("DIFF", a["cmd"]); print("  was:", a["code"], a["out"][:200], a["err"][:100]); print("  now:", b["code"], b["out"][:200], b["err"][:100])
        if len(old["runs"]) != len(result["runs"]): bad += 1; print("run count differs")
        for k in set(old["files"]) | set(result["files"]):
            if old["files"].get(k) != result["files"].get(k): bad += 1; print("FILE DIFF", k)
        print("golden:", "OK" if not bad else f"{bad} differences", f"({len(result['runs'])} runs)")
        sys.exit(1 if bad else 0)
