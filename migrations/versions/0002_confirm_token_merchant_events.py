"""Токен платежа (хранится только SHA-256) и журнал смены статуса мерчанта.

confirm_token нужен для confirm, cancel и чтения статуса: клиент анонимен, и без токена любой, кто знает
payment_id, мог бы отменить чужой платёж (ADR-003). В базе лежит только хеш, сам токен показывается один раз.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"

UPGRADE = """
ALTER TABLE payments ADD COLUMN confirm_token_hash bytea;

CREATE TABLE merchant_status_events (
    event_id    bigserial PRIMARY KEY,
    merchant_id text NOT NULL REFERENCES merchants (merchant_id),
    old_status  text NOT NULL,
    new_status  text NOT NULL,
    reason      text NOT NULL,
    actor       text NOT NULL,
    at          timestamptz NOT NULL
);
CREATE TRIGGER merchant_status_events_no_update_delete BEFORE UPDATE OR DELETE ON merchant_status_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
CREATE TRIGGER merchant_status_events_no_truncate BEFORE TRUNCATE ON merchant_status_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();
"""

DOWNGRADE = """
DROP TABLE merchant_status_events;
ALTER TABLE payments DROP COLUMN confirm_token_hash;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
