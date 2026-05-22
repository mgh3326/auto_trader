"""add crypto_candles_1m

Revision ID: e5df7fbd9803
Revises: 2efa08c3fb09
Create Date: 2026-05-20 21:50:30.432158

ROB-284 — TimescaleDB hypertable for 1-minute crypto candles. Identity is
(instrument_id, time); instrument_id FK references crypto_instruments(id).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5df7fbd9803'
down_revision: Union[str, Sequence[str], None] = '2efa08c3fb09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "crypto_candles_1m",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("base_volume", sa.Numeric(), nullable=False),
        sa.Column("quote_volume", sa.Numeric(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("vwap", sa.Numeric(), nullable=True),
        sa.Column("taker_buy_base_volume", sa.Numeric(), nullable=True),
        sa.Column("taker_buy_quote_volume", sa.Numeric(), nullable=True),
        sa.Column(
            "is_closed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["crypto_instruments.id"],
            name="fk_crypto_candles_1m_instrument",
        ),
        sa.PrimaryKeyConstraint(
            "instrument_id", "time", name="pk_crypto_candles_1m"
        ),
        sa.CheckConstraint(
            "base_volume >= 0", name="ck_crypto_candles_1m_base_volume_nn"
        ),
        sa.CheckConstraint(
            "quote_volume IS NULL OR quote_volume >= 0",
            name="ck_crypto_candles_1m_quote_volume_nn",
        ),
        sa.CheckConstraint(
            "trade_count IS NULL OR trade_count >= 0",
            name="ck_crypto_candles_1m_trade_count_nn",
        ),
        sa.CheckConstraint(
            "vwap IS NULL OR vwap >= 0",
            name="ck_crypto_candles_1m_vwap_nn",
        ),
        sa.CheckConstraint(
            "high >= low", name="ck_crypto_candles_1m_high_ge_low"
        ),
        sa.CheckConstraint(
            "high >= open AND high >= close",
            name="ck_crypto_candles_1m_high_ge_oc",
        ),
        sa.CheckConstraint(
            "low <= open AND low <= close",
            name="ck_crypto_candles_1m_low_le_oc",
        ),
    )
    op.execute(
        "SELECT create_hypertable('public.crypto_candles_1m', 'time', "
        "chunk_time_interval => INTERVAL '1 day')"
    )
    op.create_index(
        "ix_crypto_candles_1m_source_time",
        "crypto_candles_1m",
        ["source", sa.text("time DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_crypto_candles_1m_source_time", table_name="crypto_candles_1m"
    )
    op.drop_table("crypto_candles_1m")
