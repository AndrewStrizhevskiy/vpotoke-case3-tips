"""Ошибки в формате контракта: {error_code, message, retryable}; HTTP-код берётся из каталога (ADR-006)."""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tips.domain.errors import ERRORS, PaymentError


def error_body(code):
    http, retryable, message = ERRORS[code]
    return http, {"error_code": code, "message": message, "retryable": retryable}


def error_response(code, headers=None):
    http, body = error_body(code)
    return JSONResponse(body, status_code=http, headers=headers)


async def payment_error_handler(_request: Request, exc: PaymentError):
    return error_response(exc.code)


async def validation_error_handler(_request: Request, _exc: RequestValidationError):
    return error_response("VALIDATION_ERROR")


async def http_error_handler(_request: Request, exc: StarletteHTTPException):
    """Неизвестный путь или метод: тот же формат ошибки, без подробностей."""
    code = "NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR"
    http, body = error_body(code)
    return JSONResponse(body, status_code=exc.status_code if exc.status_code in (404, 405) else http)
