"""ROB-1010: Normalize crypto_instruments.venue from 'KRW' to 'upbit'

Revision ID: 20260725_rob1010_crypto_venue
Revises: 20260723_approval_dispatch
Create Date: 2026-07-25 15:50:00.000000

ROB-1010 root cause:
  Backfill migration 181f946296ff seeded crypto_instruments using:
    CASE WHEN c.market = 'upbit_krw' THEN 'upbit' ELSE c.market END AS venue
  Because legacy crypto_candles_1d.market contained 'KRW' rather than
  'upbit_krw', 261 rows were inserted into crypto_instruments with
  venue='KRW' instead of venue='upbit'.

  This caused _resolve_instrument_id() in daily_candles/repository.py
  to fail (as it queries venue='upbit' for partition='upbit_krw'), leading
  to empty candle ranges during forecast resolution (unresolved_no_data).

This migration normalizes venue='KRW' -> venue='upbit' in crypto_instruments.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260725_rob1010_crypto_venue"
down_revision: Union[str, Sequence[str], None] = "20260723_approval_dispatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Normalize legacy venue='KRW' rows to venue='upbit'."""
    op.execute(
        """
        UPDATE crypto_instruments
        SET venue = 'upbit', updated_at = NOW()
        WHERE venue = 'KRW'
          AND NOT EXISTS (
            SELECT 1 FROM crypto_instruments target
            WHERE target.venue = 'upbit'
              AND target.product = crypto_instruments.product
              AND target.venue_symbol = crypto_instruments.venue_symbol
          )
        """
    )


def downgrade() -> None:
    """Revert venue='upbit' rows back to venue='KRW'."""
    op.execute(
        """
        UPDATE crypto_instruments
        SET venue = 'KRW', updated_at = NOW()
        WHERE venue = 'upbit'
        """
    )
