"""Подключение к PostgreSQL: адрес и схема берутся из окружения (секреты не хранятся в коде)."""
import os
import re
import sys

ENV_URL = "TIPS_DATABASE_URL"
ENV_SCHEMA = "TIPS_DB_SCHEMA"
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def normalize_url(url):
    """postgresql://… -> postgresql+psycopg://… (драйвер psycopg 3)."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def database_url():
    url = os.environ.get(ENV_URL)
    if not url:
        sys.exit("Ошибка: не задана переменная окружения TIPS_DATABASE_URL")
    return normalize_url(url)


def schema_name():
    schema = os.environ.get(ENV_SCHEMA, "public")
    if not SCHEMA_RE.match(schema):
        sys.exit("Ошибка: недопустимое имя схемы в TIPS_DB_SCHEMA")
    return schema


def make_engine(url=None, schema=None, **kwargs):
    from sqlalchemy import create_engine

    schema = schema or schema_name()
    if not SCHEMA_RE.match(schema):
        raise ValueError("недопустимое имя схемы")
    address = normalize_url(url) if url else database_url()
    # hide_parameters: в тексте ошибок базы нет значений параметров запроса (подписи, хеши, токены)
    return create_engine(address, connect_args={"options": f"-c search_path={schema}"}, hide_parameters=True, **kwargs)
