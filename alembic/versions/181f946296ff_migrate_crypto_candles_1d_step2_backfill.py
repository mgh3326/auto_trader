"""migrate crypto_candles_1d step2 backfill

Revision ID: 181f946296ff
Revises: 6acbc5e7fc93
Create Date: 2026-05-20 21:53:38.063844

ROB-284 step 2 — idempotent backfill:
  - For every distinct (market, symbol) currently in crypto_candles_1d,
    ensure a crypto_instruments row exists. Today this is Upbit KRW pairs
    only (market='upbit_krw', symbol like 'KRW-XXX').
  - Populate instrument_id by JOIN on (market, symbol) -> derived
    (venue, product, venue_symbol).
  - Copy volume -> base_volume, value -> quote_volume.
  - Mark all existing rows is_closed = TRUE.

This step is re-runnable: the instrument seed uses ON CONFLICT DO NOTHING
and the backfill UPDATE filters by `WHERE c.instrument_id IS NULL`.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '181f946296ff'
down_revision: Union[str, Sequence[str], None] = '6acbc5e7fc93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Seed crypto_instruments for every distinct (market, symbol).
    #
    # Today the only producer is the Upbit KRW path: market='upbit_krw',
    # symbol like 'KRW-XXX'. Translate that into (venue='upbit',
    # product='spot', venue_symbol=symbol, base_asset=substring after 'KRW-',
    # quote_asset='KRW'). Idempotent via ON CONFLICT DO NOTHING.
    op.execute(
        """
        INSERT INTO crypto_instruments
            (venue, product, venue_symbol, base_asset, quote_asset, status)
        SELECT DISTINCT
            CASE WHEN c.market = 'upbit_krw' THEN 'upbit' ELSE c.market END
              AS venue,
            'spot' AS product,
            c.symbol AS venue_symbol,
            CASE
                WHEN c.symbol LIKE 'KRW-%' THEN substring(c.symbol from 5)
                ELSE c.symbol
            END AS base_asset,
            CASE
                WHEN c.symbol LIKE 'KRW-%' THEN 'KRW'
                ELSE 'UNKNOWN'
            END AS quote_asset,
            'active' AS status
        FROM crypto_candles_1d c
        ON CONFLICT (venue, product, venue_symbol) DO NOTHING
        """
    )

    # 2. Backfill instrument_id, base_volume, quote_volume, is_closed.
    op.execute(
        """
        UPDATE crypto_candles_1d c
        SET instrument_id = i.id,
            base_volume   = COALESCE(c.base_volume, c.volume),
            quote_volume  = COALESCE(c.quote_volume, c.value),
            is_closed     = COALESCE(c.is_closed, TRUE)
        FROM crypto_instruments i
        WHERE i.venue_symbol = c.symbol
          AND i.venue = CASE WHEN c.market = 'upbit_krw' THEN 'upbit' ELSE c.market END
          AND i.product = 'spot'
          AND c.instrument_id IS NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Reverse the backfill: clear the columns we populated. The instruments
    # themselves are kept (cheap, useful for other paths).
    op.execute(
        """
        UPDATE crypto_candles_1d
        SET instrument_id = NULL,
            base_volume   = NULL,
            quote_volume  = NULL,
            is_closed     = NULL
        """
    )
