"""Защитная обвязка вокруг приложения: идентификатор запроса, ограничение частоты, размер тела, заголовки
безопасности, журнал доступа без секретов, единый ответ на необработанные ошибки, CORS по списку.
"""
import logging
import re
import time
import uuid

from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from tips.adapters.sql.store import SqlStore
from tips.domain.errors import RateLimited
from tips.http import logs
from tips.http.errors import error_body
from tips.http.limits import client_ip

access_log = logging.getLogger("tips.access")
error_log = logging.getLogger("tips.http")

MAX_BODY = 64 * 1024  # тела запросов маленькие: больше 64 КиБ это ошибка или атака
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}
HSTS = "max-age=31536000; includeSubDomains"
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def json_error(code, status=None, headers=None):
    http, body = error_body(code)
    return JSONResponse(body, status_code=status or http, headers=headers)


def install(app, limiter, proxy_hops, production, cors_origins):
    @app.middleware("http")
    async def guard(request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
        logs.request_id.set(rid)
        started = time.perf_counter()
        ip = client_ip(request, proxy_hops)
        try:
            declared = request.headers.get("content-length", "0")
            if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY:
                response = json_error("VALIDATION_ERROR")
            elif request.url.path == "/health" or request.url.path.startswith("/api/v2/webhooks/"):
                # проверки живости и вебхуки провайдера общим лимитом не считаем (у вебхуков свой лимит в обработчике)
                response = await call_next(request)
            else:
                try:
                    await run_in_threadpool(
                        lambda: limiter.check(SqlStore(app.state.engine), "global", ip, app.state.clock()))
                except RateLimited as exc:
                    response = json_error("RATE_LIMITED", headers={"Retry-After": str(exc.retry_after)})
                else:
                    response = await call_next(request)
        except Exception:  # noqa: BLE001 - любая необработанная ошибка: пишем без секретов, клиенту общий ответ
            error_log.exception("необработанная ошибка: %s %s", request.method, request.url.path)
            response = json_error("INTERNAL_ERROR")
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and not production and request.url.path in DOCS_PATHS:
                continue  # Swagger UI в разработке подгружает скрипты и стили
            response.headers.setdefault(name, value)
        if production:
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        response.headers["X-Request-ID"] = rid
        # путь без query: подпись и токены в адресе в журнал не попадают
        access_log.info("%s %s %s %dms ip=%s", request.method, request.url.path, response.status_code,
                        (time.perf_counter() - started) * 1000, ip)
        return response

    # CORS добавляется последним, то есть снаружи: ответы самой обвязки (429, 500) тоже получают его заголовки
    if cors_origins:
        if "*" in cors_origins:
            raise ValueError("CORS: звёздочка недопустима, перечислите источники явно")
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "OPTIONS"], max_age=600,
                           allow_headers=["Authorization", "Content-Type", "X-Payment-Token", "X-Request-ID"])
