"""Проверка настроек перед запуском в production: небезопасная конфигурация не стартует, а не «работает как-нибудь».

Вызывается из python -m tips.http. Приложение, собранное тестами, этих проверок не проходит: они смотрят на окружение.
"""
import base64
import binascii

from tips.domain.qr_v2 import KID, MIN_KEY_BYTES

DEMO_SECRETS = {"demo-secret-k3"}  # публичный ключ организаторов хакатона


def is_production(environ):
    return environ.get("TIPS_ENV", "production") != "development"


def problems(environ):
    """Список проблем (пустой, если настройки допустимы для production)."""
    out = []
    if not is_production(environ):
        return out
    hmac = environ.get("TIPS_HMAC_SECRET", "")
    if hmac in DEMO_SECRETS or 0 < len(hmac) < 16:
        out.append("TIPS_HMAC_SECRET: демонстрационный или короткий ключ (нужен случайный не короче 16 символов)")
    if environ.get("TIPS_API_V1", "on").lower() != "off":
        out.append("TIPS_API_V1 должен быть off: гостевые операции v1 (подпись из 64 бит, число в JSON) в production выключены")
    if not environ.get("TIPS_QR_KEYS") or not environ.get("TIPS_QR_ACTIVE_KID"):
        out.append("TIPS_QR_KEYS и TIPS_QR_ACTIVE_KID обязательны (генератор: python -m tips.http.keys qr)")
    else:
        for item in environ["TIPS_QR_KEYS"].split(","):
            kid, _, b64 = item.strip().partition(":")
            try:
                short = len(base64.b64decode(b64, validate=True)) < MIN_KEY_BYTES
            except (binascii.Error, ValueError):
                short = True
            if not KID.match(kid) or short:
                out.append(f"TIPS_QR_KEYS: ключ «{kid}» некорректен (kid a-z0-9 до 8 символов, base64 не короче 32 байт)")
        kids = [i.strip().partition(":")[0] for i in environ["TIPS_QR_KEYS"].split(",")]
        if len(kids) != len(set(kids)):
            out.append("TIPS_QR_KEYS: kid повторяется (последний ключ молча перекрыл бы первый)")
        if environ["TIPS_QR_ACTIVE_KID"] not in kids:
            out.append("TIPS_QR_ACTIVE_KID отсутствует среди ключей TIPS_QR_KEYS")
    if environ.get("TIPS_PROVIDER", "") == "sandbox":
        out.append("TIPS_PROVIDER=sandbox запрещён в production")
    front = environ.get("TIPS_FRONT_URL", "")
    if not front.startswith("https://") or not front.rstrip("/").endswith("/p"):
        out.append("TIPS_FRONT_URL обязателен: https://<узел>/p (форму сервис отдаёт по /p)")
    if not environ.get("TIPS_JWT_KID") or not environ.get("TIPS_JWT_PRIVATE_KEY"):
        out.append("TIPS_JWT_KID и TIPS_JWT_PRIVATE_KEY обязательны (генератор: python -m tips.http.keys)")
    if not environ.get("TIPS_DATABASE_URL"):
        out.append("TIPS_DATABASE_URL обязателен")
    if environ.get("TIPS_METRICS_TOKEN") and len(environ["TIPS_METRICS_TOKEN"]) < 24:
        out.append("TIPS_METRICS_TOKEN короче 24 символов")
    for origin in (o.strip() for o in environ.get("TIPS_CORS_ORIGINS", "").split(",") if o.strip()):
        if origin == "*" or not origin.startswith("https://"):
            out.append(f"TIPS_CORS_ORIGINS: «{origin}» недопустим (только https-источники, без звёздочки)")
    return out
