"""Пользователи, токены обновления и журнал событий входа (M4).

users: роль определяет, какие идентификаторы обязательны (CHECK), а составной внешний ключ
(employee_id, merchant_id) не даёт привязать официанта к чужому заведению.
refresh_tokens: в базе только SHA-256 токена; family_id объединяет цепочку обновлений одного входа,
чтобы при повторном предъявлении уже использованного токена отозвать всю цепочку.
auth_events: только дописывается; пароли и введённые логины не записываются.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"

UPGRADE = """
CREATE TABLE users (
    user_id             text PRIMARY KEY,
    login               text NOT NULL CHECK (login ~ '^[a-z0-9._@-]{3,64}$'),
    password_hash       text NOT NULL,
    role                text NOT NULL CHECK (role IN ('merchant', 'waiter', 'admin')),
    merchant_id         text REFERENCES merchants (merchant_id),
    employee_id         text,
    status              text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    failed_attempts     integer NOT NULL DEFAULT 0,
    last_failed_at      timestamptz,
    locked_until        timestamptz,
    created_at          timestamptz NOT NULL,
    password_changed_at timestamptz NOT NULL,
    FOREIGN KEY (employee_id, merchant_id) REFERENCES employees (employee_id, merchant_id),
    CHECK ((role = 'admin'    AND merchant_id IS NULL     AND employee_id IS NULL)
        OR (role = 'merchant' AND merchant_id IS NOT NULL AND employee_id IS NULL)
        OR (role = 'waiter'   AND merchant_id IS NOT NULL AND employee_id IS NOT NULL))
);
CREATE UNIQUE INDEX users_login_key ON users (login);
CREATE UNIQUE INDEX users_one_account_per_employee ON users (employee_id) WHERE employee_id IS NOT NULL;

CREATE TABLE refresh_tokens (
    token_id          bigserial PRIMARY KEY,
    user_id           text NOT NULL REFERENCES users (user_id),
    family_id         uuid NOT NULL,
    token_hash        bytea NOT NULL UNIQUE,
    issued_at         timestamptz NOT NULL,
    expires_at        timestamptz NOT NULL,
    family_started_at timestamptz NOT NULL,
    rotated_at        timestamptz,
    revoked_at        timestamptz
);
CREATE INDEX refresh_tokens_family ON refresh_tokens (family_id);
CREATE INDEX refresh_tokens_user ON refresh_tokens (user_id);

CREATE TABLE auth_events (
    event_id bigserial PRIMARY KEY,
    user_id  text REFERENCES users (user_id),
    action   text NOT NULL CHECK (action IN
             ('login_ok', 'login_failed', 'login_locked', 'refresh_ok', 'refresh_reuse', 'logout',
              'user_created', 'password_changed', 'user_disabled', 'user_enabled')),
    at       timestamptz NOT NULL
);
CREATE TRIGGER auth_events_no_update_delete BEFORE UPDATE OR DELETE ON auth_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
CREATE TRIGGER auth_events_no_truncate BEFORE TRUNCATE ON auth_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();
"""

DOWNGRADE = """
DROP TABLE auth_events;
DROP TABLE refresh_tokens;
DROP TABLE users;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
