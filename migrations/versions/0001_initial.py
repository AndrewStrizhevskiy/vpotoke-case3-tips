"""Схема платформы чаевых: справочники, QR, чеки, платежи, аудит.

Ограничения базы заменяют логику на файлах из прототипа:
  * payments_one_success: не больше одного успешного платежа на QR (nonce гасится на SUCCESS, ADR-002);
  * payments_one_active: не больше одного платежа в CREATED или PENDING на QR (блокировка QR, ADR-002);
  * UNIQUE (qr_id, idempotency_key): идемпотентность (ADR-008);
  * audit_events: только дописывается (триггер запрещает UPDATE, DELETE и TRUNCATE).

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None

UPGRADE = """
CREATE SEQUENCE payment_seq START 101;

CREATE TABLE merchants (
    merchant_id text PRIMARY KEY,
    name        text NOT NULL,
    status      text NOT NULL CHECK (status IN ('ACTIVE', 'BLOCKED', 'PENDING')),
    description text NOT NULL DEFAULT ''
);

CREATE TABLE employees (
    employee_id text PRIMARY KEY,
    merchant_id text NOT NULL REFERENCES merchants (merchant_id),
    name        text NOT NULL,
    payout_mask text NOT NULL CHECK (payout_mask ~ '^\\*{4}[0-9]{4}$'),
    UNIQUE (employee_id, merchant_id)
);

CREATE TABLE receipts (
    merchant_id text NOT NULL REFERENCES merchants (merchant_id),
    receipt_id  text NOT NULL,
    amount      numeric(12, 2) NOT NULL CHECK (amount > 0 AND amount <= 1000000),
    currency    char(3) NOT NULL DEFAULT 'RUB',
    PRIMARY KEY (merchant_id, receipt_id)
);

CREATE TABLE qr_codes (
    qr_id       text PRIMARY KEY,
    merchant_id text NOT NULL,
    employee_id text NOT NULL,
    receipt_id  text NOT NULL,
    nonce       text NOT NULL,
    exp         timestamptz NOT NULL,
    signature   text NOT NULL,
    created_at  timestamptz NOT NULL,
    CHECK (exp <= created_at + interval '24 hours'),
    FOREIGN KEY (employee_id, merchant_id) REFERENCES employees (employee_id, merchant_id),
    FOREIGN KEY (merchant_id, receipt_id) REFERENCES receipts (merchant_id, receipt_id)
);

CREATE TABLE payments (
    payment_id      text PRIMARY KEY,
    qr_id           text NOT NULL REFERENCES qr_codes (qr_id),
    merchant_id     text NOT NULL REFERENCES merchants (merchant_id),
    employee_id     text NOT NULL REFERENCES employees (employee_id),
    amount          numeric(12, 2) NOT NULL CHECK (amount > 0 AND amount <= 1000000),
    tip             numeric(12, 2) NOT NULL CHECK (tip >= 0 AND tip <= amount * 0.5 + 0.005),
    amount_total    numeric(12, 2) NOT NULL CHECK (amount_total = amount + tip AND amount_total <= 2000000),
    currency        char(3) NOT NULL DEFAULT 'RUB',
    status          text NOT NULL CHECK (status IN ('CREATED', 'PENDING', 'SUCCESS', 'DECLINED', 'CANCELED')),
    error_code      text,
    idempotency_key text NOT NULL,
    created_at      timestamptz NOT NULL,
    submitted_at    timestamptz,
    finalized_at    timestamptz,
    version         integer NOT NULL DEFAULT 1,
    UNIQUE (qr_id, idempotency_key)
);
CREATE UNIQUE INDEX payments_one_success ON payments (qr_id) WHERE status = 'SUCCESS';
CREATE UNIQUE INDEX payments_one_active ON payments (qr_id) WHERE status IN ('CREATED', 'PENDING');
CREATE INDEX payments_merchant_created ON payments (merchant_id, created_at);
CREATE INDEX payments_employee_created ON payments (employee_id, created_at);
CREATE INDEX payments_open ON payments (status, submitted_at) WHERE status IN ('CREATED', 'PENDING');

-- исторические платежи из seed-данных: только для отчётов, QR и блокировки не затрагивают
CREATE TABLE seed_payments (
    payment_id  text PRIMARY KEY,
    merchant_id text NOT NULL,
    employee_id text NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    tip         numeric(12, 2) NOT NULL,
    status      text NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE audit_events (
    event_id    bigserial PRIMARY KEY,
    payment_id  text NOT NULL REFERENCES payments (payment_id),
    qr_id       text NOT NULL,
    action      text NOT NULL CHECK (action IN
                ('payment_created', 'payment_submitted', 'payment_confirmed', 'payment_declined', 'payment_canceled')),
    from_status text,
    to_status   text NOT NULL,
    amount_total numeric(12, 2) NOT NULL,
    actor       text NOT NULL CHECK (actor IN ('client', 'system', 'officer')),
    at          timestamptz NOT NULL
);

CREATE FUNCTION audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;
CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();
"""

DOWNGRADE = """
DROP TABLE audit_events;
DROP FUNCTION audit_events_append_only();
DROP TABLE seed_payments;
DROP TABLE payments;
DROP TABLE qr_codes;
DROP TABLE receipts;
DROP TABLE employees;
DROP TABLE merchants;
DROP SEQUENCE payment_seq;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
