"""M5: нумерация QR последовательностью и счётчики лимитов запросов.

qr_seq: номер QR раньше вычислялся как «последний + 1», и два одновременных выпуска получали один номер.
rate_hits: счётчики фиксированных окон для ограничения частоты запросов по адресу клиента (хранятся в той же
базе, отдельный Redis не нужен при небольшой нагрузке); старые окна удаляет фоновая задача.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"

UPGRADE = """
CREATE SEQUENCE qr_seq START 101;

CREATE TABLE rate_hits (
    bucket       text NOT NULL,
    window_start timestamptz NOT NULL,
    hits         integer NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);
CREATE INDEX rate_hits_window ON rate_hits (window_start);
"""

DOWNGRADE = """
DROP TABLE rate_hits;
DROP SEQUENCE qr_seq;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
