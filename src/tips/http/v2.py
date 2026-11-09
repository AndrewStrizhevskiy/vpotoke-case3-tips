"""API v2: непрозрачные идентификаторы, деньги строками, QR версии 2, платёж через провайдера, вебхуки.

Гостевые операции, выпуск QR и вебхуки. Персонал (вход, отчёты, статус мерчанта) пока общий для обеих версий и живёт
под /api/v1. Те же сервисы, что у v1 (create_payment_core, провайдерский поток); v2 проверяет формат запроса сам и
принимает только QR версии 2 (понижения версии нет в обе стороны).
"""
import re

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from tips.adapters.provider.base import ProviderUnavailable, WebhookInvalid
from tips.app.payments import cancel_payment, create_payment_core
from tips.app.provider_flow import accept_webhook, confirm_payment_v2, refresh_payment
from tips.app.qr import V2Issue, create_qr, get_qr_view_v2
from tips.app.tokens import hash_token, new_token, token_matches
from tips.domain import ids
from tips.domain.errors import PaymentError
from tips.domain.money import fmt, parse_money_v2
from tips.domain.qr_v2 import qr_version, verify_v2
from tips.http import auth
from tips.http.errors import error_body

MERCHANT = re.compile(r"\Am_[0-9a-z]{3,32}\Z")
EMPLOYEE = re.compile(r"\Ae_[0-9a-z]{3,32}\Z")
RECEIPT = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
CARD_TOKEN = re.compile(r"\A[A-Za-z0-9_-]{8,128}\Z")
SIG = re.compile(r"\A[A-Za-z0-9_-]{22}\Z")
STATUS_CODE = {"SUCCESS": 200, "PENDING": 202, "DECLINED": 402}
PAYMENT_KEYS = {"qr_id", "sig", "receipt_id", "base_amount", "tip_percent", "tip_amount"}


def payment_json(p, token=None):
    body = {"payment_id": p["payment_id"], "qr_id": p["qr_id"], "status": p["status"], "base_amount": p["amount"],
            "tip_amount": p["tip"], "amount_total": p["amount_total"], "currency": "RUB",
            "error_code": p["error_code"] or None, "next_action": p.get("next_action") or None,
            "created_at": p["timestamp"]}
    if token is not None:
        body["confirm_token"] = token
    return body


def install(app, h):
    """h: помощники приложения (store, rate, шлюзы подписей, principal, read_json, invalid, matches)."""
    state = app.state

    def unavailable():
        _, body = error_body("INTERNAL_ERROR")
        return JSONResponse(body, status_code=503, headers={"Retry-After": "30", **h.no_store})

    def owned(payment_id, token):
        """Платёж API v2 по id и токену; чужой токен, чужая версия и несуществующий id дают один NOT_FOUND."""
        if not h.matches(ids.PAYMENT_V2, payment_id):
            raise h.invalid()
        s = h.store()
        p = s.get_payment(payment_id)
        if p is None or int(p.get("api_version", 1)) != 2 or not token_matches(token, s.confirm_token_hash(payment_id)):
            raise PaymentError("NOT_FOUND")
        return s, p

    def payment_token(request):
        token = request.headers.get("x-payment-token")
        if not token:
            raise h.invalid()
        return token

    # ---------- персонал: выпуск QR ----------

    @app.post("/api/v2/qr", status_code=201)
    async def issue_qr_v2(request: Request):
        who = await h.principal(request)
        body = await h.read_json(request)
        if (not isinstance(body, dict) or set(body) != {"merchant_id", "employee_id", "receipt_id"}
                or not h.matches(MERCHANT, body["merchant_id"]) or not h.matches(EMPLOYEE, body["employee_id"])
                or not h.matches(RECEIPT, body["receipt_id"])):
            raise h.invalid()
        auth.authorize_issue_qr(who, body["merchant_id"], body["employee_id"])
        issuer = V2Issue(state.qr_keys, state.front_url)
        result = await run_in_threadpool(lambda: create_qr(
            body, state.secret, h.store(), state.clock(), amount_required=False, v2=issuer))
        return {"qr_id": result["qr_id"], "pay_url": result["pay_url"], "exp": result["exp"]}

    # ---------- гость ----------

    @app.get("/api/v2/qr/{qr_id}")
    async def qr_view(qr_id: str, request: Request, s: str = ""):
        await h.rate(request, "qr_view")
        await h.bad_signature_gate(request, qr_id)
        try:
            if not h.matches(ids.QR_V2, qr_id) or not h.matches(SIG, s):
                raise PaymentError("INVALID_QR")
            return await run_in_threadpool(lambda: get_qr_view_v2(qr_id, s, state.qr_keys, h.store(), state.clock()))
        except PaymentError as exc:
            if exc.code == "INVALID_QR":
                await h.note_bad_signature(request, qr_id)
            raise

    @app.post("/api/v2/payments")
    async def create_tip_v2(request: Request):
        key = request.headers.get("idempotency-key", "")
        if not ids.IDEMPOTENCY_KEY_V2.match(key):  # ключ работает как секрет повтора: только UUIDv4 в каноническом виде
            raise h.invalid()
        body = await h.read_json(request)
        if (not isinstance(body, dict) or not set(body) <= PAYMENT_KEYS or not h.matches(ids.QR_V2, body.get("qr_id"))
                or not isinstance(body.get("sig"), str) or ("tip_percent" in body) == ("tip_amount" in body)
                or "base_amount" not in body):
            raise h.invalid()
        qr_id = body["qr_id"]
        base = parse_money_v2(body["base_amount"])
        tip_in = parse_money_v2(body["tip_amount"]) if "tip_amount" in body else None
        tip_pct = body.get("tip_percent")
        if "tip_percent" in body and (isinstance(tip_pct, bool) or not isinstance(tip_pct, int)):
            raise h.invalid()
        if "receipt_id" in body and not h.matches(RECEIPT, body["receipt_id"]):
            raise h.invalid()
        await h.rate(request, "tip_create", qr_id)
        await h.bad_signature_gate(request, qr_id)

        def work():
            s = h.store()
            qr = s.find_qr(qr_id)
            amount = s.receipt_amount(qr["merchant_id"], qr["receipt_id"]) if qr else None
            if (qr is None or qr_version(qr) != 2 or amount is None
                    or not verify_v2(state.qr_keys, qr, fmt(amount), "RUB", body["sig"])):
                raise PaymentError("INVALID_QR")
            with s.atomic():  # платёж и хеш токена записываются вместе
                payment, replay = create_payment_core(s, qr, state.clock(), key, body.get("receipt_id", qr["receipt_id"]),
                                                      base, tip_pct, tip_in, lambda: ids.new_id("pay"), 2)
                token = new_token()  # при повторе выдаётся новый токен, старый перестаёт работать
                s.set_confirm_token(payment["payment_id"], hash_token(token))
            return payment, replay, token

        try:
            payment, replay, token = await run_in_threadpool(work)
        except PaymentError as exc:
            if exc.code == "INVALID_QR":
                await h.note_bad_signature(request, qr_id)
            raise
        return JSONResponse(payment_json(payment, token), status_code=200 if replay else 201)

    @app.get("/api/v2/payments/{payment_id}")
    async def payment_status_v2(payment_id: str, request: Request):
        token = payment_token(request)
        await h.rate(request, "pay_status", payment_id if h.matches(ids.PAYMENT_V2, payment_id) else "invalid")

        def work():
            s, p = owned(payment_id, token)
            if p["status"] == "PENDING" and state.provider is not None and p["provider"] == state.provider.name:
                try:  # опрос сам обновляет платёж у провайдера: 3-D Secure закрывается и без вебхука
                    p = refresh_payment(s, p, state.provider, state.clock())
                except ProviderUnavailable:
                    pass
            return p

        return payment_json(await run_in_threadpool(work))

    @app.post("/api/v2/payments/{payment_id}/confirm")
    async def confirm_v2(payment_id: str, request: Request):
        token = payment_token(request)
        await h.rate(request, "pay_action", payment_id if h.matches(ids.PAYMENT_V2, payment_id) else "invalid")
        body = await h.read_json(request)
        card = body.get("payment_method_token") if isinstance(body, dict) else None
        if not isinstance(card, str) or not CARD_TOKEN.match(card) or set(body) != {"payment_method_token"}:
            raise h.invalid()
        if state.provider is None:
            return unavailable()

        def work():
            s, p = owned(payment_id, token)
            return confirm_payment_v2(p, s, state.provider, card, f"{state.front_url}/result", state.clock())

        p = await run_in_threadpool(work)
        return JSONResponse(payment_json(p), status_code=STATUS_CODE.get(p["status"], 200))

    @app.post("/api/v2/payments/{payment_id}/cancel")
    async def cancel_v2(payment_id: str, request: Request):
        token = payment_token(request)
        await h.rate(request, "pay_action", payment_id if h.matches(ids.PAYMENT_V2, payment_id) else "invalid")

        def work():
            s, p = owned(payment_id, token)
            return cancel_payment(p, s, state.clock())

        return payment_json(await run_in_threadpool(work))

    # ---------- вебхуки провайдера ----------

    @app.post("/api/v2/webhooks/{provider}")
    async def webhook(provider: str, request: Request):
        if state.provider is None or provider != state.provider.name:
            raise PaymentError("NOT_FOUND")
        await h.rate(request, "webhook")  # свой щедрый лимит: общий по адресу задушил бы всплески событий
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > h.max_body:
                raise h.invalid()
        headers = dict(request.headers)

        def work():
            return accept_webhook(h.store(), state.provider, headers, bytes(raw), state.clock())

        try:
            await run_in_threadpool(work)
        except WebhookInvalid:  # подпись неверна или устарела: причина наружу не сообщается
            raise h.invalid()
        return Response(status_code=200, headers=h.no_store)
