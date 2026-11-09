"""Журналы без секретов (ADR-009): подписи, nonce, токены, пароли и номера карт не должны попадать в логи.

Каждая строка журнала проходит через redact(): даже если секрет случайно окажется в тексте исключения или в
параметре запроса, он будет замаскирован. Основная защита другая: мы не пишем тела запросов и параметры адреса,
а в строке доступа только метод, путь без query, код ответа, время, адрес и идентификатор запроса.
Управляющие символы в тексте сообщения экранируются: иначе путь с %0A подделал бы вторую строку журнала.
"""
import contextvars
import copy
import logging
import re

request_id = contextvars.ContextVar("request_id", default="-")

QUERY_SECRETS = re.compile(r"(?i)([?&](?:sig|signature|confirm_token|refresh_token|access_token|token|password)=)[^&\s\"']+")
KEYED_SECRETS = re.compile(
    r"(?i)([\"']?(?:password|refresh_token|access_token|confirm_token|idempotency_key|nonce|signature|sig|"
    r"authorization|x-payment-token|secret|private_key)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)")
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")  # длинные токены, хеши, ключи любой кодировки (hex, base64url)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits):
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
        flip = not flip
    return total % 10 == 0


def _mask_card(match):
    digits = re.sub(r"\D", "", match.group(0))
    return f"****{digits[-4:]}" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def redact(text):
    text = QUERY_SECRETS.sub(r"\1[masked]", text)
    text = BEARER.sub(r"\1[masked]", text)  # раньше «ключ: значение», иначе замаскируется слово Bearer, а не токен
    text = KEYED_SECRETS.sub(r"\1[masked]", text)
    text = JWT.sub("[masked-jwt]", text)
    text = TOKEN.sub("[masked-token]", text)
    return CARD.sub(_mask_card, text)


def escape_controls(text):
    return CONTROL.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text.replace("\r", "\\r").replace("\n", "\\n"))


class RedactingFormatter(logging.Formatter):
    """Очищает уже собранную строку журнала вместе с трейсбеком. Запись не изменяется: другие обработчики
    (например, отправка ошибок в систему мониторинга) получают исходное исключение."""

    def format(self, record):
        clone = copy.copy(record)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - неверные аргументы сообщения не должны ронять вызывающий код
            message = str(record.msg)
        clone.msg, clone.args = escape_controls(redact(message)), ()
        return redact(super().format(clone))


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


def install(level=logging.INFO):
    """Единый формат и очистка для всех обработчиков корневого журнала."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
