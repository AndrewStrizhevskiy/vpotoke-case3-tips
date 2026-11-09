#!/usr/bin/env python3
"""
CLI-прототип платформы чаевых — кейс 3.

Команды (первый аргумент; без команды выполняется pay):
    pay      qr.json      create + confirm за один запуск
    create   qr.json      проверки -> платёж в статусе CREATED
    confirm  payment_id   CREATED -> PENDING -> итог эмулятора
    cancel   payment_id   CREATED -> CANCELED (иначе CANCEL_NOT_ALLOWED; повтор отмены — 200)
    reconcile             сверка: PENDING старше 15 минут -> DECLINED (GATEWAY_TIMEOUT)
    qr       req.json     выпуск QR на чек: nonce, срок 24 ч, подпись, платёжная ссылка (FR-1)
    report   --merchant|--employee --from --to [--daily]   отчёты по payments (ADR-007)

Параметры: --now ISO-время UTC, --qr-file путь к qr_links.csv, --out-dir каталог для *_out.csv.
Секрет подписи берётся только из переменной окружения TIPS_HMAC_SECRET.

Коды выхода: 0 — операция успешна (SUCCESS, CREATED, CANCELED), 1 — платёж не SUCCESS
(DECLINED или PENDING), 2 — ошибка проверки или запроса.

Решения: docs/adr-001 (эмулятор), 002 (статусы), 003 (подпись и сумма чека),
004 (порядок проверок, Decimal), 005 (колонки), 008 (идемпотентность), 009 (секрет, время).
"""
import os
import sys

# Логика живёт в пакете src/tips; здесь только точка входа, чтобы команды и тесты остались прежними.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tips.cli.main import main  # noqa: E402
from tips.domain.errors import ERRORS  # noqa: E402,F401  (contract_check.py читает ERRORS отсюда)

if __name__ == "__main__":
    main()
