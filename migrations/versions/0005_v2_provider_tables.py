"""API v2 и адаптер провайдера: версия QR, поля платежа у провайдера, входящие события, возвраты.

Только добавления: существующие строки остаются действительными (QR получают version=1, платежи api_version=1).
Расширяются списки допустимых статусов и действий аудита.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"

UPGRADE = """
ALTER TABLE qr_codes ADD COLUMN version smallint NOT NULL DEFAULT 1 CHECK (version IN (1, 2));
ALTER TABLE qr_codes ADD COLUMN key_id text;
ALTER TABLE qr_codes ADD CONSTRAINT qr_codes_version_key CHECK (
    (version = 1 AND key_id IS NULL) OR (version = 2 AND key_id IS NOT NULL));

ALTER TABLE payments ADD COLUMN api_version smallint NOT NULL DEFAULT 1 CHECK (api_version IN (1, 2));
ALTER TABLE payments ADD COLUMN provider text;
ALTER TABLE payments ADD COLUMN provider_payment_id text;
ALTER TABLE payments ADD COLUMN next_action jsonb;
ALTER TABLE payments ADD COLUMN provider_status text;
CREATE UNIQUE INDEX payments_provider_ref ON payments (provider, provider_payment_id)
    WHERE provider_payment_id IS NOT NULL;
ALTER TABLE payments DROP CONSTRAINT payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN
    ('CREATED', 'PENDING', 'SUCCESS', 'DECLINED', 'CANCELED', 'REFUND_PENDING', 'REFUNDED'));

ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check CHECK (action IN
    ('payment_created', 'payment_submitted', 'payment_confirmed', 'payment_declined', 'payment_canceled',
     'payment_late_success', 'payment_refund_requested', 'payment_refunded'));

CREATE TABLE gateway_events (
    provider            text NOT NULL,
    event_id            text NOT NULL,
    event_type          text NOT NULL,
    provider_payment_id text,
    payload             jsonb NOT NULL,
    received_at         timestamptz NOT NULL,
    processed_at        timestamptz,
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    PRIMARY KEY (provider, event_id)
);
CREATE INDEX gateway_events_unprocessed ON gateway_events (received_at) WHERE processed_at IS NULL;

CREATE TABLE refunds (
    refund_id          text PRIMARY KEY,
    payment_id         text NOT NULL REFERENCES payments (payment_id),
    provider_refund_id text,
    amount             numeric(12, 2) NOT NULL CHECK (amount > 0),
    reason             text NOT NULL CHECK (reason IN ('DOUBLE_PAYMENT', 'MANUAL')),
    status             text NOT NULL CHECK (status IN ('REQUIRED', 'PENDING', 'SUCCEEDED', 'FAILED')),
    attempts           integer NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL
);
CREATE UNIQUE INDEX refunds_one_double_payment ON refunds (payment_id) WHERE reason = 'DOUBLE_PAYMENT';
CREATE INDEX refunds_open ON refunds (status, updated_at) WHERE status IN ('REQUIRED', 'PENDING');
"""

DOWNGRADE = """
DROP TABLE refunds;
DROP TABLE gateway_events;
ALTER TABLE audit_events DROP CONSTRAINT audit_events_action_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_action_check CHECK (action IN
    ('payment_created', 'payment_submitted', 'payment_confirmed', 'payment_declined', 'payment_canceled'));
ALTER TABLE payments DROP CONSTRAINT payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN
    ('CREATED', 'PENDING', 'SUCCESS', 'DECLINED', 'CANCELED'));
DROP INDEX payments_provider_ref;
ALTER TABLE payments DROP COLUMN provider_status, DROP COLUMN next_action, DROP COLUMN provider_payment_id,
    DROP COLUMN provider, DROP COLUMN api_version;
ALTER TABLE qr_codes DROP CONSTRAINT qr_codes_version_key;
ALTER TABLE qr_codes DROP COLUMN key_id, DROP COLUMN version;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
