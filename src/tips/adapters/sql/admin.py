"""Администрирование схемы: миграции Alembic и seed. Командная строка: python -m tips.adapters.sql.admin."""
import os
import sys

from tips.adapters.sql.engine import database_url, make_engine, schema_name
from tips.config import DATA_DIR, REPO_ROOT


def migrate(engine=None, schema=None):
    """Применить миграции в схему (alembic upgrade head); схема должна совпадать со схемой движка."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations"))
    cfg.attributes["schema"] = schema or schema_name()
    cfg.attributes["engine"] = engine or make_engine(schema=cfg.attributes["schema"])
    command.upgrade(cfg, "head")


def bootstrap(url=None, schema=None, seed_data=True):
    """Создать схему, применить миграции и (по умолчанию) загрузить seed. Возвращает движок."""
    from tips.adapters.sql.seed import seed

    schema = schema or schema_name()
    engine = make_engine(url, schema)
    migrate(engine, schema)
    if seed_data:
        seed(engine, DATA_DIR)
    return engine


def drop_schema(url, schema):
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    command = argv[0] if argv else ""
    if command == "migrate":
        migrate(make_engine(database_url(), schema_name()))
        print("миграции применены")
    elif command == "seed":
        from tips.adapters.sql.seed import seed

        seed(make_engine(database_url(), schema_name()), DATA_DIR)
        print("seed загружен")
    else:
        sys.exit("использование: python -m tips.adapters.sql.admin migrate|seed")


if __name__ == "__main__":
    main()
