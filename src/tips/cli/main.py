"""Командная строка: pay, create, confirm, cancel, reconcile, qr, report (см. prototype/04_прототип.py)."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from tips.adapters.csv_store import CsvStore
from tips.adapters.gateway.emulator import EmulatorGateway
from tips.adapters.qr_png import QrencodeRenderer
from tips.app.payments import cancel_payment, confirm_payment, create_payment, find_payment
from tips.app.qr import create_qr
from tips.app.reports import build_report
from tips.config import DATA_DIR, PROTOTYPE_DIR, get_secret
from tips.domain.errors import ERRORS, PaymentError
from tips.domain.qr_sign import parse_time
from tips.jobs.reconcile import reconcile


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
    parser.add_argument("--out-dir", default=PROTOTYPE_DIR)
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
    store = CsvStore(ns.out_dir, DATA_DIR, ns.qr_file)
    gateway = EmulatorGateway()

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
                emit(create_qr(req, secret, store, now, QrencodeRenderer(store.png_dir)))
                sys.exit(0)
            payment, replay = create_payment(req, secret, store, now)
            if cmd == "pay" and not replay:
                payment = confirm_payment(payment, store, gateway, now)
        elif cmd == "confirm":
            payment = confirm_payment(find_payment(store, rest[0]), store, gateway, now)
        else:
            payment = cancel_payment(find_payment(store, rest[0]), store, now)
    except PaymentError as e:
        http, retryable, message = ERRORS[e.code]
        emit({"status": "ERROR", "error_code": e.code, "message": message, "retryable": retryable, "http": http})
        sys.exit(2)

    emit(payment_json(payment))
    sys.exit(exit_code(payment["status"]))
