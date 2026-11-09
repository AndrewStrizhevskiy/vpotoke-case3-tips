"""HTTP API по контракту api/05_api_schemas.yaml: тонкий слой над сервисами src/tips/app.

Тела запросов читаются как есть и проверяются теми же функциями, что в CLI (суммы идут в Decimal, а не во float).
Ответы и коды ошибок совпадают с контрактом; клиентские операции анонимны, остальные требуют Principal (auth.py).
"""
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from tips.adapters.gateway.emulator import EmulatorGateway
from tips.adapters.sql.engine import make_engine
from tips.adapters.sql.store import SqlStore
from tips.app.payments import cancel_payment, confirm_payment, create_payment
from tips.app.qr import create_qr, get_qr_view
from tips.app.reports import build_report
from tips.app.tokens import hash_token, new_token, token_matches
from tips.config import get_secret
from tips.domain.errors import PaymentError
from tips.http import auth
from tips.http.errors import error_body, http_error_handler, payment_error_handler, validation_error_handler
from tips.http.serialize import payment_json, qr_view_json

QR_ID = re.compile(r"^qr_[0-9]{3}$")
PAYMENT_ID = re.compile(r"^p_[0-9]{4}$")
MERCHANT_ID = re.compile(r"^m_[0-9]{3}$")
EMPLOYEE_ID = re.compile(r"^e_[0-9]{3}$")
SIG = re.compile(r"^[a-f0-9]{16}$")
STATUS_BY_PAYMENT = {"SUCCESS": 200, "PENDING": 202, "DECLINED": 402}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def invalid():
    return PaymentError("VALIDATION_ERROR")


async def read_json(request):
    """Тело запроса как объект; дробные числа остаются Decimal, чтобы 637.005 не превратилось в 637.0 молча."""
    try:
        return json.loads(await request.body(), parse_float=Decimal)
    except ValueError:
        raise invalid()


def matches(pattern, value):
    return isinstance(value, str) and pattern.match(value) is not None


def create_app(engine=None, gateway=None, secret=None, clock=None):
    """Собрать приложение. Параметры нужны тестам: свой движок, шлюз, ключ и часы (QR из seed истекли в 2025 году)."""
    app = FastAPI(title="Tips Platform", version="1.0.0", description="Онлайн-чаевые для сети «Вкус-Сервис»")
    app.state.engine = engine or make_engine()
    app.state.gateway = gateway or EmulatorGateway()
    key = secret if secret is not None else get_secret()
    app.state.secret = key.encode() if isinstance(key, str) else key
    app.state.clock = clock or utc_now

    app.add_exception_handler(PaymentError, payment_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)

    @app.exception_handler(auth.Unauthenticated)
    async def unauthenticated(_request, _exc):
        _, body = error_body("ACCESS_DENIED")
        return JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    def store():
        return SqlStore(app.state.engine)

    def principal(request):
        return auth.authenticate(request.headers)

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
        return {"status": "ok"}

    # ---------- клиент (без авторизации) ----------

    @app.get("/api/v1/qr/{qr_id}")
    async def qr_for_form(qr_id: str, sig: str = ""):
        if not matches(QR_ID, qr_id) or not matches(SIG, sig):
            raise PaymentError("INVALID_QR")  # как и неверная подпись: не подсказываем, что именно не так
        view = await run_in_threadpool(lambda: get_qr_view(qr_id, sig, app.state.secret, store(), app.state.clock()))
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

        def work():
            s = store()
            with s.atomic():  # платёж и хеш токена записываются вместе
                payment, replay = create_payment(req, app.state.secret, s, app.state.clock())
                token = new_token()  # при повторе выдаётся новый токен: старый показывался один раз и мог потеряться
                s.set_confirm_token(payment["payment_id"], hash_token(token))
            return payment, replay, token

        payment, replay, token = await run_in_threadpool(work)
        return JSONResponse(payment_json(payment, token), status_code=200 if replay else 201)

    @app.get("/api/v1/payments/{payment_id}")
    async def payment_status(payment_id: str, request: Request):
        token = request.headers.get("x-payment-token")
        if not token:
            raise invalid()
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
        token = await token_from_body(request)

        def work():
            s, payment = owned_payment(payment_id, token)
            return confirm_payment(payment, s, app.state.gateway, app.state.clock())

        payment = await run_in_threadpool(work)
        return JSONResponse(payment_json(payment), status_code=STATUS_BY_PAYMENT.get(payment["status"], 200))

    @app.post("/api/v1/payments/{payment_id}/cancel")
    async def cancel(payment_id: str, request: Request):
        token = await token_from_body(request)

        def work():
            s, payment = owned_payment(payment_id, token)
            return cancel_payment(payment, s, app.state.clock())

        return payment_json(await run_in_threadpool(work))

    # ---------- мерчант, официант, администратор ----------

    @app.post("/api/v1/qr", status_code=201)
    async def issue_qr(request: Request):
        who = principal(request)
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
        who = principal(request)
        check_ids(merchant_id, employee_id)
        if (merchant_id is None) == (employee_id is None):  # ровно один из двух
            raise invalid()
        auth.authorize_report(who, merchant_id, employee_id)
        first, last = request.query_params.get("from"), request.query_params.get("to")
        return await run_in_threadpool(lambda: build_report(store(), merchant_id, employee_id, first, last, False))

    @app.get("/api/v1/reports/tips/daily")
    async def tips_daily(request: Request, merchant_id: Optional[str] = None):
        who = principal(request)
        check_ids(merchant_id, None)
        if merchant_id is None:
            raise invalid()
        auth.authorize_report(who, merchant_id, None)
        first, last = request.query_params.get("from"), request.query_params.get("to")
        return await run_in_threadpool(lambda: build_report(store(), merchant_id, None, first, last, True))

    @app.patch("/api/v1/merchants/{merchant_id}/status")
    async def merchant_status(merchant_id: str, request: Request):
        who = principal(request)
        auth.authorize_admin(who)
        if not matches(MERCHANT_ID, merchant_id):
            raise invalid()
        body = await read_json(request)
        reason = body.get("reason") if isinstance(body, dict) else None
        status = body.get("status") if isinstance(body, dict) else None
        if status not in ("ACTIVE", "BLOCKED") or not isinstance(reason, str) or not 1 <= len(reason) <= 200:
            raise invalid()
        old = await run_in_threadpool(
            lambda: store().set_merchant_status(merchant_id, status, reason, f"admin:{who.role}", app.state.clock()))
        if old is None:
            raise PaymentError("NOT_FOUND")
        return {"merchant_id": merchant_id, "status": status}

    return app
