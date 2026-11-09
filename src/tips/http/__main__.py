"""Запуск сервиса: TIPS_DATABASE_URL, TIPS_HMAC_SECRET, по желанию TIPS_HOST и TIPS_PORT (по контракту 8083)."""
import os

import uvicorn

from tips.http.app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("TIPS_HOST", "127.0.0.1"), port=int(os.environ.get("TIPS_PORT", "8083")))
