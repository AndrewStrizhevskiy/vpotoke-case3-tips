"""Запуск сервиса: TIPS_DATABASE_URL, TIPS_HMAC_SECRET, TIPS_JWT_*, по желанию TIPS_HOST, TIPS_PORT (по контракту 8083).

Журнал доступа ведёт само приложение (путь без query, адрес, код, время): стандартный access-лог uvicorn выключен,
потому что в нём был бы адрес целиком, а в нём подпись QR.
"""
import os

import uvicorn

from tips.http import logs
from tips.http.app import create_app

if __name__ == "__main__":
    logs.install()
    uvicorn.run(create_app(), host=os.environ.get("TIPS_HOST", "127.0.0.1"), port=int(os.environ.get("TIPS_PORT", "8083")),
                access_log=False, log_config=None)
