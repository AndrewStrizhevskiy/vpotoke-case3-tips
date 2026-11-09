"""HTTP API по контракту api/05_api_schemas.yaml: тонкий слой над сервисами src/tips/app.

Тела запросов читаются как есть и проверяются теми же функциями, что в CLI (суммы идут в Decimal, а не во float).
Ответы и коды ошибок совпадают с контрактом; клиентские операции анонимны, остальные требуют Principal (auth.py).
"""
import json
import os
import re
import types
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from tips.adapters.gateway.emulator import EmulatorGateway
from tips.adapters.jwt_keys import Keyring
from tips.adapters.passwords import Passwords
from tips.adapters.sql.engine import make_engine
from tips.adapters.sql.store import SqlStore
from tips.app import auth as auth_service
from tips.app.payments import cancel_payment, confirm_payment, create_payment
from tips.app.qr import create_qr, get_qr_view
from tips.app.reports import build_report
from tips.app.tokens import hash_token, new_token, token_matches
from tips.config import get_secret
from tips.domain.qr_sign import as_keys
from tips.domain.errors import PaymentError, RateLimited, Unauthenticated
from tips.adapters.provider.factory import make_provider
from tips.domain import ids
from tips.domain.qr_v2 import QrV2Keys
from tips.http import auth, guard, v2
from tips.http.limits import Limiter, client_ip, proxy_hops_from_env
from tips.http.errors import error_body, http_error_handler, payment_error_handler, validation_error_handler
from tips.http.serialize import payment_json, qr_view_json

QR_ID = re.compile(r"^qr_[0-9]{3,12}$")
PAYMENT_ID = re.compile(r"^p_[0-9]{4,12}$")
MERCHANT_ID = re.compile(r"^m_[0-9]{3}$")
EMPLOYEE_ID = re.compile(r"^e_[0-9]{3}$")
SIG = re.compile(r"^[a-f0-9]{16}$")
STATUS_BY_PAYMENT = {"SUCCESS": 200, "PENDING": 202, "DECLINED": 402}
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}  # токены не кладут в кэш


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def invalid():
    return PaymentError("VALIDATION_ERROR")


async def read_json(request):
    """Тело запроса как объект; дробные числа остаются Decimal, чтобы 637.005 не превратилось в 637.0 молча.
    Читается потоком с ограничением размера: заявленная длина может быть неправдой, а тела без длины бывают."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > guard.MAX_BODY:
            raise invalid()
    try:
        return json.loads(bytes(body), parse_float=Decimal)
    except ValueError:
        raise invalid()


def is_qr_id(value):
    """Идентификатор QR любой версии: ключ блокировки по неверным подписям не должен зависеть от формата."""
    return matches(QR_ID, value) or matches(ids.QR_V2, value)


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None  # fullmatch: «$» пропускал бы \n в конце


def create_app(engine=None, gateway=None, secret=None, clock=None, keyring=None, passwords=None,
               limiter=None, proxy_hops=None, production=None, cors_origins=None, qr_keys=None, provider=None,
               front_url=None, enable_v1=True):
    """Собрать приложение. Параметры нужны тестам: свой движок, шлюз, ключи, часы (QR из seed истекли в 2025 году)
    и лимиты. Настройки среды: TIPS_ENV (production по умолчанию: документация API и HSTS; development открывает
    /docs), TIPS_TRUSTED_PROXY_HOPS (сколько прокси перед сервисом), TIPS_CORS_ORIGINS (список источников)."""
    if production is None:
        production = os.environ.get("TIPS_ENV", "production") != "development"
    if cors_origins is None:
        cors_origins = [o.strip() for o in os.environ.get("TIPS_CORS_ORIGINS", "").split(",") if o.strip()]
    docs = {} if not production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Tips Platform", version="1.0.0", description="Онлайн-чаевые для сети «Вкус-Сервис»", **docs)
    app.state.engine = engine or make_engine()
    app.state.gateway = gateway or EmulatorGateway()
    key = secret if secret is not None else get_secret()
    app.state.secret = as_keys(key.encode() if isinstance(key, str) else key)
    app.state.clock = clock or utc_now
    app.state.keyring = keyring or Keyring.from_env()
    app.state.passwords = passwords or Passwords()
    app.state.limiter = limiter or Limiter()
    app.state.proxy_hops = proxy_hops if proxy_hops is not None else proxy_hops_from_env()
    # API v2 включается, когда заданы ключи QR v2 (TIPS_QR_KEYS); адрес формы TIPS_FRONT_URL; провайдер TIPS_PROVIDER
    app.state.qr_keys = qr_keys or (QrV2Keys.from_env() if os.environ.get("TIPS_QR_KEYS") else None)
    app.state.provider = provider if provider is not None else (make_provider(production) if app.state.qr_keys else None)
    app.state.front_url = (front_url or os.environ.get("TIPS_FRONT_URL", "https://tips.example/p")).rstrip("/")
    app.add_exception_handler(PaymentError, payment_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)

    @app.exception_handler(RateLimited)
    async def rate_limited(_request, exc):
        _, body = error_body("RATE_LIMITED")
        return JSONResponse(body, status_code=429, headers={"Retry-After": str(exc.retry_after), **NO_STORE})

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(_request, _exc):
        _, body = error_body("ACCESS_DENIED")
        return JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer", **NO_STORE})

    def store():
        return SqlStore(app.state.engine)

    def ip_of(request):
        return client_ip(request, app.state.proxy_hops)

    async def rate(request, rule, key=None):
        """Учесть запрос по правилу; ключ всегда начинается с адреса клиента."""
        ip = ip_of(request)
        full = ip if key is None else f"{ip}:{key}"
        await run_in_threadpool(lambda: app.state.limiter.check(store(), rule, full, app.state.clock()))

    async def bad_signature_gate(request, qr_id):
        """5 неверных подписей по одной паре «адрес и qr_id» блокируют её на 15 минут: подпись не подобрать."""
        if is_qr_id(qr_id):
            key = f"{ip_of(request)}:{qr_id}"
            await run_in_threadpool(lambda: app.state.limiter.blocked(store(), "bad_sig", key, app.state.clock()))

    async def note_bad_signature(request, qr_id):
        if is_qr_id(qr_id):
            key = f"{ip_of(request)}:{qr_id}"
            await run_in_threadpool(lambda: app.state.limiter.record(store(), "bad_sig", key, app.state.clock()))

    async def principal(request):
        """Кто вызывает: проверенный JWT и действующий пользователь в базе. Иначе 401."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthenticated()
        who = await run_in_threadpool(
            lambda: auth_service.authenticate(store(), app.state.keyring, token.strip(), app.state.clock()))
        await run_in_threadpool(
            lambda: app.state.limiter.check(store(), "protected", who.user_id, app.state.clock()))
        return who

    def owned_payment(payment_id, token):
        """Платёж по id и токену. Чужой токен и несуществующий id дают один и тот же NOT_FOUND."""
        if not matches(PAYMENT_ID, payment_id):
            raise invalid()
        s = store()
        payment = s.get_payment(payment_id)
        if payment is None or not token_matches(token, s.confirm_token_hash(payment_id)):
            raise PaymentError("NOT_FOUND")
        return s, payment

    # ---------- служебное ----------

    @app.get("/health")
    def health():
        """Живость и доступность базы: балансировщик не должен слать запросы туда, где база недоступна."""
        try:
            store().ping()
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "degraded"}, status_code=503, headers=NO_STORE)
        return {"status": "ok"}

    # ---------- клиент (без авторизации) ----------

    @app.get("/api/v1/qr/{qr_id}")
    async def qr_for_form(qr_id: str, request: Request, sig: str = ""):
        await rate(request, "qr_view")
        await bad_signature_gate(request, qr_id)
        try:
            if not matches(QR_ID, qr_id) or not matches(SIG, sig):
                raise PaymentError("INVALID_QR")  # как и неверная подпись: не подсказываем, что именно не так
            view = await run_in_threadpool(lambda: get_qr_view(qr_id, sig, app.state.secret, store(), app.state.clock()))
        except PaymentError as exc:
            if exc.code == "INVALID_QR":
                await note_bad_signature(request, qr_id)
            raise
        return qr_view_json(view)

    @app.post("/api/v1/tips")
    async def create_tip(request: Request):
        req = await read_json(request)
        if not isinstance(req, dict):
            raise invalid()
        key = req.get("idempotency_key")
        try:
            req["idempotency_key"] = str(uuid.UUID(key))  # ключ строго UUID: он же и секрет повтора
        except (ValueError, AttributeError, TypeError):
            raise invalid()
        if not isinstance(req.get("sig"), str):
            raise invalid()
        qr_id = req.get("qr_id") if matches(QR_ID, req.get("qr_id")) else "invalid"
        await rate(request, "tip_create", qr_id)
        await bad_signature_gate(request, qr_id)

        def work():
            s = store()
            with s.atomic():  # платёж и хеш токена записываются вместе
                payment, replay = create_payment(req, app.state.secret, s, app.state.clock())
                token = new_token()  # при повторе выдаётся новый токен: старый показывался один раз и мог потеряться
                s.set_confirm_token(payment["payment_id"], hash_token(token))
            return payment, replay, token

        try:
            payment, replay, token = await run_in_threadpool(work)
        except PaymentError as exc:
            if exc.code == "INVALID_QR":
                await note_bad_signature(request, qr_id)
            raise
        return JSONResponse(payment_json(payment, token), status_code=200 if replay else 201)

    @app.get("/api/v1/payments/{payment_id}")
    async def payment_status(payment_id: str, request: Request):
        token = request.headers.get("x-payment-token")
        if not token:
            raise invalid()
        await rate(request, "pay_status", payment_id if matches(PAYMENT_ID, payment_id) else "invalid")
        _, payment = await run_in_threadpool(lambda: owned_payment(payment_id, token))
        return payment_json(payment)

    async def token_from_body(request):
        body = await read_json(request)
        token = body.get("confirm_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or len(token) < 32:
            raise invalid()
        return token

    @app.post("/api/v1/payments/{payment_id}/confirm")
    async def confirm(payment_id: str, request: Request):
        await rate(request, "pay_action", payment_id if matches(PAYMENT_ID, payment_id) else "invalid")
        token = await token_from_body(request)

        def work():
            s, payment = owned_payment(payment_id, token)
            return confirm_payment(payment, s, app.state.gateway, app.state.clock())

        payment = await run_in_threadpool(work)
        return JSONResponse(payment_json(payment), status_code=STATUS_BY_PAYMENT.get(payment["status"], 200))

    @app.post("/api/v1/payments/{payment_id}/cancel")
    async def cancel(payment_id: str, request: Request):
        await rate(request, "pay_action", payment_id if matches(PAYMENT_ID, payment_id) else "invalid")
        token = await token_from_body(request)

        def work():
            s, payment = owned_payment(payment_id, token)
            return cancel_payment(payment, s, app.state.clock())

        return payment_json(await run_in_threadpool(work))

    # ---------- вход ----------

    async def body_object(request):
        body = await read_json(request)
        if not isinstance(body, dict):
            raise invalid()
        return body

    @app.post("/api/v1/auth/login")
    async def login(request: Request):
        await rate(request, "login")
        body = await body_object(request)
        pair = await run_in_threadpool(lambda: auth_service.login(
            store(), app.state.keyring, app.state.passwords, body.get("login"), body.get("password"), app.state.clock()))
        return JSONResponse(pair, headers=NO_STORE)

    @app.post("/api/v1/auth/refresh")
    async def refresh(request: Request):
        await rate(request, "refresh")
        body = await body_object(request)
        pair = await run_in_threadpool(lambda: auth_service.refresh(
            store(), app.state.keyring, body.get("refresh_token"), app.state.clock()))
        return JSONResponse(pair, headers=NO_STORE)

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request):
        await rate(request, "refresh")
        body = await body_object(request)
        if not isinstance(body.get("refresh_token"), str):
            raise invalid()
        await run_in_threadpool(lambda: auth_service.logout(store(), body["refresh_token"], app.state.clock()))
        return Response(status_code=204, headers=NO_STORE)

    # ---------- мерчант, официант, администратор ----------

    @app.post("/api/v1/qr", status_code=201)
    async def issue_qr(request: Request):
        who = await principal(request)
        req = await read_json(request)
        if not isinstance(req, dict) or "amount" in req:  # сумма чека берётся из реестра кассы, а не из запроса
            raise invalid()
        for key, pattern in (("merchant_id", MERCHANT_ID), ("employee_id", EMPLOYEE_ID)):
            if not matches(pattern, req.get(key)):
                raise invalid()
        auth.authorize_issue_qr(who, req["merchant_id"], req["employee_id"])
        result = await run_in_threadpool(
            lambda: create_qr(req, app.state.secret, store(), app.state.clock(), amount_required=False))
        return {"qr_id": result["qr_id"], "pay_url": result["pay_url"], "exp": result["exp"]}

    def check_ids(merchant_id, employee_id):
        for pattern, value in ((MERCHANT_ID, merchant_id), (EMPLOYEE_ID, employee_id)):
            if value is not None and not matches(pattern, value):
                raise invalid()

    @app.get("/api/v1/reports/tips")
    async def tips_report(request: Request, merchant_id: Optional[str] = None, employee_id: Optional[str] = None):
        who = await principal(request)
        check_ids(merchant_id, employee_id)
        if (merchant_id is None) == (employee_id is None):  # ровно один из двух
            raise invalid()
        auth.authorize_report(who, merchant_id, employee_id)
        first, last = request.query_params.get("from"), request.query_params.get("to")
        return await run_in_threadpool(lambda: build_report(store(), merchant_id, employee_id, first, last, False))

    @app.get("/api/v1/reports/tips/daily")
    async def tips_daily(request: Request, merchant_id: Optional[str] = None):
        who = await principal(request)
        check_ids(merchant_id, None)
        if merchant_id is None:
            raise invalid()
        auth.authorize_report(who, merchant_id, None)
        first, last = request.query_params.get("from"), request.query_params.get("to")
        return await run_in_threadpool(lambda: build_report(store(), merchant_id, None, first, last, True))

    @app.patch("/api/v1/merchants/{merchant_id}/status")
    async def merchant_status(merchant_id: str, request: Request):
        who = await principal(request)
        auth.authorize_admin(who)
        if not matches(MERCHANT_ID, merchant_id):
            raise invalid()
        body = await read_json(request)
        reason = body.get("reason") if isinstance(body, dict) else None
        status = body.get("status") if isinstance(body, dict) else None
        if status not in ("ACTIVE", "BLOCKED") or not isinstance(reason, str) or not 1 <= len(reason) <= 200:
            raise invalid()
        old = await run_in_threadpool(
            lambda: store().set_merchant_status(merchant_id, status, reason, f"admin:{who.user_id}", app.state.clock()))
        if old is None:
            raise PaymentError("NOT_FOUND")
        return {"merchant_id": merchant_id, "status": status}

    if app.state.qr_keys is not None:
        v2.install(app, types.SimpleNamespace(
            store=store, rate=rate, bad_signature_gate=bad_signature_gate, note_bad_signature=note_bad_signature,
            principal=principal, read_json=read_json, invalid=invalid, matches=matches, no_store=NO_STORE,
            max_body=guard.MAX_BODY))
    if not enable_v1:  # после перехода клиентов на v2 гостевые операции v1 отключаются; вход и отчёты остаются
        v1_guest = {"/api/v1/qr", "/api/v1/qr/{qr_id}", "/api/v1/tips", "/api/v1/payments/{payment_id}",
                    "/api/v1/payments/{payment_id}/confirm", "/api/v1/payments/{payment_id}/cancel"}
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) not in v1_guest]
    guard.install(app, app.state.limiter, app.state.proxy_hops, production, cors_origins)
    return app
