"""Каталог ошибок: код -> (HTTP-аналог, retryable, текст для клиента)."""


ERRORS = {
    "VALIDATION_ERROR": (400, False, "Неверный формат запроса"),
    "INVALID_QR": (422, False, "Не удалось открыть оплату"),
    "QR_EXPIRED": (410, True, "QR устарел, попросите официанта новый"),
    "QR_USED": (409, False, "Этот чек уже оплачен"),
    "MERCHANT_BLOCKED": (403, False, "Оплата в заведении недоступна"),
    "RECEIPT_MISMATCH": (422, False, "Не удалось открыть оплату"),
    "AMOUNT_MISMATCH": (422, False, "Сумма чека изменилась, обновите страницу"),
    "AMOUNT_OUT_OF_RANGE": (422, False, "Некорректная сумма"),
    "TIP_OUT_OF_RANGE": (422, True, "Чаевые — не больше 50 % от чека"),
    "IDEMPOTENCY_CONFLICT": (409, False, "Обновите страницу"),
    "PAYMENT_IN_PROGRESS": (409, True, "Оплата уже обрабатывается"),
    "CANCEL_NOT_ALLOWED": (409, False, "Отмена невозможна: оплата уже отправлена в банк или закрыта"),
    "PAYMENT_STATE_CONFLICT": (409, False, "Оплата была отменена"),
    "NOT_FOUND": (404, False, "Не удалось открыть оплату"),
    "ACCESS_DENIED": (403, False, "Нет доступа"),
}  # код -> (HTTP-аналог, retryable, текст для клиента); совпадает с docs/adr-006 и api/05_api_schemas.yaml


class PaymentError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
