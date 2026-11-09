"""Фоновый процесс: сверка зависших платежей, автоотмена забытых, чистка счётчиков лимитов.

    python -m tips.jobs.worker [--once] [--interval 30]

Запускается отдельно от HTTP-сервиса (нужны TIPS_DATABASE_URL и, для шлюза, его настройки). Несколько копий
безопасны: каждый переход статуса условный, проигравший гонку пропускает платёж. Журнал без секретов: только
номера платежей и счётчики.
"""
import argparse
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone

from tips.adapters.gateway.emulator import EmulatorGateway
from tips.adapters.sql.engine import make_engine
from tips.adapters.sql.store import SqlStore
from tips.http import logs
from tips.jobs.reconcile import cancel_stale, reconcile

log = logging.getLogger("tips.worker")
KEEP_RATE_WINDOWS = timedelta(hours=2)  # самое длинное окно лимитов 15 минут


def run_once(store, gateway, now):
    """Один проход всех задач. Отказ одной задачи не мешает остальным."""
    report = {}
    details = {}
    for name, job in (("reconciled", lambda: reconcile(store, gateway, now, details)),
                      ("canceled", lambda: cancel_stale(store, gateway, now, details=details)),
                      ("purged_rate_windows", lambda: store.purge_rate_hits(now - KEEP_RATE_WINDOWS))):
        try:
            report[name] = job()
        except Exception:  # noqa: BLE001 - задача упала: пишем и продолжаем со следующей
            log.exception("задача %s завершилась ошибкой", name)
            report[name] = "error"
    if details.get("stuck"):
        log.error("платежи в PENDING дольше суток, нужен разбор вручную: %s", ", ".join(details["stuck"]))
    if details.get("stuck_created"):
        log.error("платежи в CREATED дольше суток, отменить у шлюза не удаётся: %s", ", ".join(details["stuck_created"]))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Фоновые задачи платформы чаевых")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--interval", type=float, default=30.0, help="пауза между проходами, секунды")
    ns = parser.parse_args(argv)
    logs.install()  # тот же формат и очистка секретов, что у HTTP-сервиса
    store, gateway = SqlStore(make_engine()), EmulatorGateway()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        report = run_once(store, gateway, datetime.now(timezone.utc).replace(microsecond=0))
        log.info("проход: %s", {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
        if ns.once:
            break
        stop.wait(ns.interval)


if __name__ == "__main__":
    main()
