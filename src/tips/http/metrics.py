"""Метрики для мониторинга (формат Prometheus, текст) из состояния базы: работают при любом числе процессов.

GET /metrics включается переменной TIPS_METRICS_TOKEN (Authorization: Bearer <токен>); без неё маршрута нет (404).
Токен сравнивается за постоянное время. Метрики не содержат идентификаторов платежей, сумм и персональных данных.
"""
import hmac
import os

from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from tips.domain.errors import PaymentError

STATUSES = ("CREATED", "PENDING", "SUCCESS", "DECLINED", "CANCELED", "REFUND_PENDING", "REFUNDED")


def render(snap):
    lines = ["# HELP tips_payments Платежи по статусам", "# TYPE tips_payments gauge"]
    by = snap["payments"]
    lines += [f'tips_payments{{status="{s}"}} {by.get(s, 0)}' for s in STATUSES]
    gauges = [
        ("tips_oldest_pending_seconds", "Возраст самого старого PENDING (сверка закрывает за 15 минут)", snap["oldest_pending"]),
        ("tips_oldest_created_seconds", "Возраст самого старого CREATED (автоотмена через 30 минут)", snap["oldest_created"]),
        ("tips_webhook_backlog", "Необработанные события провайдера", snap["inbox_backlog"]),
        ("tips_webhook_stuck", "События провайдера, исчерпавшие попытки обработки", snap["inbox_stuck"]),
        ("tips_refunds_open", "Возвраты в работе", snap["refunds_open"]),
        ("tips_refunds_stuck", "Возвраты, исчерпавшие попытки: нужна ручная работа", snap["refunds_stuck"]),
    ]
    for name, help_text, value in gauges:
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {int(value)}"]
    return "\n".join(lines) + "\n"


def install(app, h, token):
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        given = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise PaymentError("NOT_FOUND")  # без токена маршрут неотличим от несуществующего
        snap = await run_in_threadpool(lambda: h.store().metrics_snapshot(app.state.clock()))
        return PlainTextResponse(render(snap), headers=h.no_store)


def token_from_env():
    return os.environ.get("TIPS_METRICS_TOKEN", "")
