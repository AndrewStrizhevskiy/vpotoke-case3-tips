"""Alembic: адрес базы и схема берутся из окружения (TIPS_DATABASE_URL, TIPS_DB_SCHEMA)."""
import os
import sys

from alembic import context
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tips.adapters.sql.engine import make_engine  # noqa: E402


def run_migrations():
    schema = context.config.attributes.get("schema") or os.environ.get("TIPS_DB_SCHEMA", "public")
    engine = context.config.attributes.get("engine") or make_engine(schema=schema)
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()
        context.configure(connection=connection, target_metadata=None, version_table_schema=schema)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
