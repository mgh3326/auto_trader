"""migrate crypto_candles_1d step1 add columns

Revision ID: 6acbc5e7fc93
Revises: e5df7fbd9803
Create Date: 2026-05-20 21:53:37.526479

ROB-284 step 1 — add new nullable columns (instrument_id, base_volume,
quote_volume, is_closed, source_event_at) to the legacy crypto_candles_1d
table. Reversible. The destructive cut from legacy (symbol, market) shape
is performed in step 3 only after step 2's backfill is verified.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6acbc5e7fc93'
down_revision: Union[str, Sequence[str], None] = 'e5df7fbd9803'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "crypto_candles_1d",
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("base_volume", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("quote_volume", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column(
            "is_closed",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("TRUE"),
        ),
    )
    op.add_column(
        "crypto_candles_1d",
        sa.Column("source_event_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_crypto_candles_1d_instrument_time",
        "crypto_candles_1d",
        ["instrument_id", sa.text("time DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_crypto_candles_1d_instrument_time",
        table_name="crypto_candles_1d",
    )
    op.drop_column("crypto_candles_1d", "source_event_at")
    op.drop_column("crypto_candles_1d", "is_closed")
    op.drop_column("crypto_candles_1d", "quote_volume")
    op.drop_column("crypto_candles_1d", "base_volume")
    op.drop_column("crypto_candles_1d", "instrument_id")
