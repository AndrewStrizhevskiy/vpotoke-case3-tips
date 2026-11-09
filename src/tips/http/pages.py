"""Страницы: форма чаевых (/p) и песочница банка (/sandbox, только вне production).

У страниц своя политика безопасности (CSP): скрипты и стили только с нашего узла, соединения только с ним, формы
никуда не отправляются. Общая политика API (`default-src 'none'`) здесь не подходит. Для живого провайдера в
`script-src`, `connect-src` и `frame-src` нужно добавить его узел (TIPS_PROVIDER_CSP, список через пробел).
"""
import html
import logging
import os
import re
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tips.adapters.provider.base import WebhookInvalid
from tips.app.provider_flow import accept_webhook
from tips.config import REPO_ROOT
from tips.domain.errors import PaymentError

WEB_DIR = os.path.join(REPO_ROOT, "web", "pay")
FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
         "style.css": "text/css; charset=utf-8"}
BANK_ID = re.compile(r"\Asb_[0-9a-f]{20}\Z")
EXP = re.compile(r"\A(0[1-9]|1[0-2])/[0-9]{2}\Z")
log = logging.getLogger("tips.sandbox")
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def csp(extra=""):
    extra = " ".join(h for h in extra.split() if re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?", h))
    frames = extra or "'none'"
    return (f"default-src 'none'; script-src 'self' {extra}; style-src 'self'; connect-src 'self' {extra}; "
            f"img-src 'self'; frame-src {frames}; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def install(app, h, sandbox=False):
    extra = os.environ.get("TIPS_PROVIDER_CSP", "")
    form_csp = csp(extra)
    assets = {}
    for name in FILES:  # читаются один раз при старте: подмена файла на диске не меняет уже запущенный сервис
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            assets[name] = f.read()
    flag = b"1" if sandbox else b"0"  # поля карты показываются только в песочнице
    assets["index.html"] = assets["index.html"].replace(b"__SANDBOX__", flag)

    def serve(name):
        return Response(assets[name], media_type=FILES[name], headers={**PAGE_HEADERS, "Content-Security-Policy": form_csp})

    @app.get("/p", include_in_schema=False)
    @app.get("/p/", include_in_schema=False)
    @app.get("/p/result", include_in_schema=False)
    async def form_page():
        return serve("index.html")

    @app.get("/p/app.js", include_in_schema=False)
    async def form_js():
        return serve("app.js")

    @app.get("/p/style.css", include_in_schema=False)
    async def form_css():
        return serve("style.css")


def install_sandbox(app, h):
    """Песочный банк в нашем же процессе: поля карты, страница 3-D Secure, отправка вебхуков. Только вне production."""
    state = app.state
    bank = state.provider.bank
    sandbox_csp = "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"

    def page(title, body):
        text = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{html.escape(title)}</title><link rel="stylesheet" href="/p/style.css"><main><h1>Песочница банка</h1>'
                f"<section>{body}</section></main></html>")
        return HTMLResponse(text, headers={**PAGE_HEADERS, "Content-Security-Policy": sandbox_csp})

    def same_origin(request):
        """Форма чужого сайта не должна дёргать песочницу: браузер присылает Origin, и он должен быть нашим."""
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            raise h.invalid()

    def flush():
        """Доставить накопленные события банка нашему вебхуку (как сделал бы сам банк)."""
        sent = 0
        for headers, body in bank.drain_webhooks():
            try:
                accept_webhook(h.store(), state.provider, headers, body, state.clock())
                sent += 1
            except WebhookInvalid:
                pass
            except Exception:  # noqa: BLE001 - остальные события из очереди не теряем
                log.exception("песочница: событие не принято")
        return sent

    @app.post("/sandbox/tokens", include_in_schema=False)
    async def tokens(request: Request):
        await h.rate(request, "pay_action", "sandbox-token")
        body = await h.read_json(request)
        if (not isinstance(body, dict) or set(body) != {"number", "exp", "cvc"}
                or not all(isinstance(body[k], str) for k in body)
                or not re.fullmatch(r"[0-9]{12,19}", body["number"]) or not EXP.match(body["exp"])
                or not re.fullmatch(r"[0-9]{3,4}", body["cvc"])):
            raise h.invalid()
        return JSONResponse({"payment_method_token": bank.create_token(body["number"])}, headers=h.no_store)

    @app.get("/sandbox", include_in_schema=False)
    @app.get("/sandbox/", include_in_schema=False)
    async def index():
        rows = "".join(f"<tr><td>{n}</td><td>{html.escape(t)}</td></tr>" for n, t in (
            ("4242 4242 4242 4242", "успех"), ("4000 0000 0000 0002", "отказ банка"),
            ("4000 0000 0000 3220", "проверка 3-D Secure"), ("4000 0000 0000 0119", "банк списал, но ответ потерян")))
        return page("Песочница", f"<h2>Тестовые карты</h2><table>{rows}</table><p>Срок и CVC любые, например 12/30 и 123.</p>"
                    '<form method="post" action="/sandbox/settle">'
                    '<button class="primary">Банк ответил на потерянные платежи</button></form>')

    @app.post("/sandbox/settle", include_in_schema=False)
    async def settle(request: Request):
        same_origin(request)
        bank.settle_all(True)
        await run_in_threadpool(flush)
        return RedirectResponse("/sandbox", status_code=303)

    @app.get("/sandbox/3ds/{bank_id}", include_in_schema=False)
    async def challenge(bank_id: str):
        if not BANK_ID.match(bank_id) or bank_id not in bank.payments:
            raise PaymentError("NOT_FOUND")
        pay = bank.payments[bank_id]
        form = (f'<h2>Подтверждение платежа</h2><p>Сумма: {html.escape(pay["amount"])} ₽. Тестовая страница 3-D Secure.</p>'
                f'<form method="post" action="/sandbox/3ds/{bank_id}">'
                '<button class="primary" name="result" value="ok">Подтвердить</button>'
                '<button class="secondary" name="result" value="fail">Отклонить</button></form>')
        return page("3-D Secure", form)

    @app.post("/sandbox/3ds/{bank_id}", include_in_schema=False)
    async def challenge_done(bank_id: str, request: Request):
        if not BANK_ID.match(bank_id) or bank_id not in bank.payments:
            raise PaymentError("NOT_FOUND")
        same_origin(request)
        raw = await request.body()
        if len(raw) > 1024:
            raise h.invalid()
        choice = parse_qs(raw.decode("utf-8", "replace")).get("result", [""])[0]
        if choice not in ("ok", "fail"):
            raise h.invalid()
        bank.complete_challenge(bank_id, choice == "ok")
        await run_in_threadpool(flush)
        return RedirectResponse(f"{state.front_url}/result", status_code=303)
