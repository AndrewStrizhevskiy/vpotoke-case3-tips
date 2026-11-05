#!/usr/bin/env python3
"""
Сквозная демонстрация прототипа одной командой (слайд 6, защита).

    export TIPS_HMAC_SECRET=<демо-ключ из README>
    python3 prototype/demo.py            # --color принудительно включает цвета

Все данные пишутся во временный каталог, файлы репозитория не меняются.
Сценарии: выпуск QR, успех, повтор, отказ, подделка подписи, таймаут и сверка, отмена, отчёт, аудит.
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(BASE_DIR, "04_прототип.py")
SEED_QR = os.path.join(BASE_DIR, "01_данные", "qr_links.csv")
NOW = "2025-11-01T18:00:00Z"
LATER = "2025-11-01T18:15:00Z"  # через 15 минут: сверка закроет зависший платёж
COLOR = sys.stdout.isatty() or "--color" in sys.argv

CODES = {"bold": "1", "dim": "90", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}
STATUS_COLOR = {"SUCCESS": "green", "CREATED": "green", "CANCELED": "green", "DECLINED": "red",
                "PENDING": "yellow", "ERROR": "red"}


def paint(text, *styles):
    if not COLOR:
        return text
    return "".join(f"\033[{CODES[s]}m" for s in styles) + text + "\033[0m"


def seed_sig(qr_id):
    with open(SEED_QR, encoding="utf-8") as f:
        return next(r["signature"] for r in csv.DictReader(f) if r["qr_id"] == qr_id)


def rows(tmp, name="payments_out.csv"):
    path = os.path.join(tmp, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(tmp, *args, now=NOW):
    cmd = [sys.executable, "-I", PROTO, "--now", now, "--out-dir", tmp] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else {}


def write_req(tmp, **body):
    path = os.path.join(tmp, f"req_{len(os.listdir(tmp))}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f)
    return path


def describe(out):
    """Одна строка о результате; пути временного каталога скрываются."""
    if out.get("status") == "ERROR":
        return "ERROR", f"ОШИБКА {out['error_code']} (HTTP {out['http']})"
    if "pay_url" in out:
        png = f", PNG: qr_png/{out['qr_id']}.png" if out.get("png") else ""
        return "SUCCESS", f"{out['qr_id']}  новый={'да' if out['new'] else 'нет'}  до {out['exp']}{png}\n    {out['pay_url']}"
    if "closed" in out:
        return "SUCCESS", f"сверка закрыла: {', '.join(out['closed']) or 'ничего'}"
    if "daily" in out:
        days = "; ".join(f"{d['date'][5:]}: {d['tips_total']:.2f} ₽ ({d['payments_count']})" for d in out["daily"])
        return "SUCCESS", f"{out['merchant_id']}  по дням: {days}"
    if "top_day" in out:
        return "SUCCESS", (f"{out['employee_id']}  {out['period']}: чаевые {out['tips_total']:.2f} ₽, платежей {out['payments_count']}, "
                           f"средний чек чаевых {out['avg_tip_amount']:.2f} ₽, лучший день {out['top_day']}")
    if "tips_total" in out:
        return "SUCCESS", (f"{out['merchant_id']}  {out['period']}: чаевые {out['tips_total']:.2f} ₽, платежей {out['payments_count']}, "
                           f"средний процент {out['avg_tip_pct']}, доля успешных {out['success_rate']}")
    error = f"  {out['error_code']}" if out.get("error_code") else ""
    return out["status"], (f"{out['payment_id']}  {out['status']}  чаевые {out['tip_amount']:.2f}  "
                           f"итого {out['amount_total']:.2f}{error}")


def step(number, title, note):
    print()
    print(paint(f"▶ {number}. {title}", "bold", "cyan"))
    print(paint(f"  {note}", "dim"))


def show(shown_cmd, code, out):
    status, text = describe(out)
    print(paint(f"  $ {shown_cmd}", "blue"))
    first, *rest = text.split("\n")
    print("  " + paint(first, STATUS_COLOR.get(status, "bold"), "bold") + paint(f"   [код выхода {code}]", "dim"))
    for line in rest:
        print("  " + line)


def main():
    if not os.environ.get("TIPS_HMAC_SECRET"):
        sys.exit("Задайте TIPS_HMAC_SECRET (демо-ключ организаторов, см. README)")
    print(paint("Онлайн-чаевые — демонстрация прототипа", "bold"))
    print(paint(f"время в демо зафиксировано: {NOW}; данные пишутся во временный каталог", "dim"))
    tmp = tempfile.mkdtemp(prefix="tips_demo_")

    step(1, "Выпуск QR на чек", "касса передаёт чек r_2001 на 1250.50 ₽, официант Иванов И. (e_001)")
    code, qr = run(tmp, "qr", write_req(tmp, merchant_id="m_001", employee_id="e_001", receipt_id="r_2001", amount=1250.50))
    show("qr req.json", code, qr)

    step(2, "Успешная оплата (T3-1)", "qr_001, чек 637.00 ₽, чаевые 5 % = 31.85, итог 668.85")
    ok_req = write_req(tmp, qr_id="qr_001", sig=seed_sig("qr_001"), receipt_id="r_1001", base_amount=637.00,
                       tip_pct=5, idempotency_key="demo-1")
    code, out = run(tmp, "pay", ok_req)
    show("pay req.json", code, out)

    step(3, "Повтор того же запроса (T3-4)", "клиент нажал «Оплатить» дважды: тот же платёж, новой записи нет")
    code, out = run(tmp, "pay", ok_req)
    show("pay req.json", code, out)
    print(f"  строк в payments_out.csv: {len(rows(tmp))}")

    step(4, "Отказ шлюза (T3-2)", "qr_003, чек 911.00 ₽, чаевые 15 % = 136.65, итог 1047.65")
    code, out = run(tmp, "pay", write_req(tmp, qr_id="qr_003", sig=seed_sig("qr_003"), receipt_id="r_1003",
                                          base_amount=911.00, tip_pct=15, idempotency_key="demo-2"))
    show("pay req.json", code, out)

    step(5, "Подделка подписи QR (T3-3)", "последняя цифра подписи изменена: платёж не создаётся")
    sig = seed_sig("qr_001")
    bad = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    code, out = run(tmp, "pay", write_req(tmp, qr_id="qr_001", sig=bad, receipt_id="r_1001", base_amount=637.00,
                                          tip_pct=5, idempotency_key="demo-3"))
    show("pay req.json", code, out)
    print(f"  строк в payments_out.csv: {len(rows(tmp))} (без изменений)")

    step(6, "Таймаут шлюза и сверка", "qr_010: шлюз не ответил; отмена запрещена; через 15 минут сверка закрывает платёж")
    code, out = run(tmp, "pay", write_req(tmp, qr_id="qr_010", sig=seed_sig("qr_010"), receipt_id="r_1010",
                                          base_amount=1870.00, tip_pct=10, idempotency_key="demo-4"))
    show("pay req.json", code, out)
    stuck = out["payment_id"]
    code, out = run(tmp, "cancel", stuck)
    show(f"cancel {stuck}", code, out)
    code, out = run(tmp, "reconcile", now=LATER)
    show("reconcile   # через 15 минут", code, out)

    step(7, "Отмена до подтверждения (T3-5)", "платёж по QR из шага 1: создан и отменён, шлюз не вызывался")
    qr_sig = qr["pay_url"].split("sig=")[1]
    code, created = run(tmp, "create", write_req(tmp, qr_id=qr["qr_id"], sig=qr_sig, receipt_id="r_2001",
                                                 base_amount=1250.50, tip_pct=10, idempotency_key="demo-5"))
    show("create req.json", code, created)
    code, out = run(tmp, "cancel", created["payment_id"])
    show(f"cancel {created['payment_id']}", code, out)

    step(8, "Отчёты за ноябрь 2025: заведение, официант, дни", "по исходному payments.csv: чаевые только по успешным платежам")
    clean = tempfile.mkdtemp(prefix="tips_demo_seed_")
    code, out = run(clean, "report", "--merchant", "m_001", "--from", "2025-11-01", "--to", "2025-11-30")
    show("report --merchant m_001 --from 2025-11-01 --to 2025-11-30", code, out)
    code, out = run(clean, "report", "--employee", "e_001", "--from", "2025-11-01", "--to", "2025-11-30")
    show("report --employee e_001 --from 2025-11-01 --to 2025-11-30", code, out)
    code, out = run(clean, "report", "--merchant", "m_001", "--daily", "--from", "2025-11-01", "--to", "2025-11-30")
    show("report --merchant m_001 --daily --from 2025-11-01 --to 2025-11-30", code, out)

    step(9, "Журнал аудита", "каждый переход статуса — отдельное событие")
    for ev in rows(tmp, "audit_out.csv"):
        move = f"{ev['from_status'] or '—'} → {ev['to_status']}"
        print(f"  {ev['event_id']}  {ev['payment_id']}  {ev['action']:<18} {move:<22} {ev['actor']}")
    print()
    print(paint("Демонстрация завершена. Файлы репозитория не изменялись.", "bold", "green"))


if __name__ == "__main__":
    main()
