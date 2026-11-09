"""Администрирование: миграции Alembic, seed и учётные записи.

    python -m tips.adapters.sql.admin migrate | seed
    python -m tips.adapters.sql.admin create-user --login L --role admin|merchant|waiter [--merchant m_001] [--employee e_001]
    python -m tips.adapters.sql.admin set-password --login L
    python -m tips.adapters.sql.admin disable-user --login L
    python -m tips.adapters.sql.admin enable-user --login L

Пароль берётся из TIPS_NEW_PASSWORD или запрашивается без вывода на экран; в аргументах и журналах его нет.
"""
import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

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
    elif command in ("create-user", "set-password", "disable-user", "enable-user"):
        manage_user(command, argv[1:])
    else:
        sys.exit("использование: python -m tips.adapters.sql.admin "
                 "migrate|seed|create-user|set-password|disable-user|enable-user")


def read_password():
    password = os.environ.get("TIPS_NEW_PASSWORD")
    if password is None:
        password = getpass.getpass("Пароль: ")
        if getpass.getpass("Повторите пароль: ") != password:
            sys.exit("Ошибка: пароли не совпадают")
    return password


def manage_user(command, args):
    from tips.adapters.passwords import Passwords
    from tips.adapters.sql.store import SqlStore
    from tips.app import users

    parser = argparse.ArgumentParser(prog=command)
    parser.add_argument("--login", required=True)
    parser.add_argument("--role")
    parser.add_argument("--merchant")
    parser.add_argument("--employee")
    ns = parser.parse_args(args)
    store = SqlStore(make_engine(database_url(), schema_name()))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        if command == "create-user":
            user_id = users.create_user(store, Passwords(), ns.login, read_password(), ns.role, ns.merchant,
                                        ns.employee, now)
            print(f"пользователь создан: {user_id}")
        elif command == "set-password":
            users.set_password(store, Passwords(), ns.login, read_password(), now)
            print("пароль изменён, все входы пользователя отозваны")
        elif command == "enable-user":
            users.enable_user(store, ns.login, now)
            print("пользователь снова действует; войти он сможет по прежнему паролю")
        else:
            users.disable_user(store, ns.login, now)
            print("пользователь закрыт, все входы отозваны")
    except ValueError as exc:  # UserError и ошибка базы при создании
        sys.exit(f"Ошибка: {exc}")


if __name__ == "__main__":
    main()
