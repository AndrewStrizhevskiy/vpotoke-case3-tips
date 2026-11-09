"""Выбор хранилища: TIPS_STORE=csv (по умолчанию) или sql (PostgreSQL, TIPS_DATABASE_URL)."""
import os
import sys

from tips.adapters.csv_store import CsvStore
from tips.config import DATA_DIR


def make_store(out_dir, qr_file):
    kind = os.environ.get("TIPS_STORE", "csv")
    if kind == "csv":
        return CsvStore(out_dir, DATA_DIR, qr_file)
    if kind == "sql":
        try:
            from tips.adapters.sql.engine import make_engine
            from tips.adapters.sql.store import SqlStore
        except ImportError:
            sys.exit("Ошибка: для TIPS_STORE=sql нужны sqlalchemy и psycopg (pip install 'sqlalchemy>=2' 'psycopg[binary]')")
        return SqlStore(make_engine(), out_dir)
    sys.exit("Ошибка: TIPS_STORE должен быть csv или sql")
