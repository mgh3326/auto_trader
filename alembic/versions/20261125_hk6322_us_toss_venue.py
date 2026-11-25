"""hk 6322: Toss US day-market minutes in research.us_candles_1m (expand step).

Revision ID: 20261125_hk6322_us_toss_venue
Revises: 20261124_hk6148_futures_minutes
Create Date: 2026-11-25

Additive. ``public.*_candles_1m`` are untouched.

1. ``research.us_candles_1m`` venue CHECK also admits ``TOSS_INTEGRATED``, the one
   integrated Toss feed (the execution venue is not known, so it is never written
   as NASD/NYSE/AMEX). The constraint is replaced NOT VALID: every existing row
   already satisfies the old list, which is a subset of the new one, so no scan
   runs under ACCESS EXCLUSIVE. The follow-up revision
   ``20261125_hk6322_us_venue_val`` runs VALIDATE CONSTRAINT.
2. ``research.us_candles_1m.value`` drops NOT NULL. Toss 1m candles carry no
   per-bar trade value, so Toss rows store NULL (unknown is neither 0 nor an
   estimate). KIS US rows keep their value.

Downgrade refuses while any row has venue ``TOSS_INTEGRATED`` (the narrowed CHECK
would fail, and the rows are first-receipt history) or while any row has a NULL
``value`` (NOT NULL cannot be restored). Nothing is dropped.
"""

# expand-contract: contract-step 6322
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261125_hk6322_us_toss_venue"
down_revision: str | Sequence[str] | None = "20261124_hk6148_futures_minutes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

US_VENUE_CHECK = "ck_research_us_candles_1m_venue"
US_VENUES_OLD = ("NASD", "NYSE", "AMEX")
US_VENUES_NEW = (*US_VENUES_OLD, "TOSS_INTEGRATED")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE research.us_candles_1m DROP CONSTRAINT IF EXISTS {US_VENUE_CHECK}"
    )
    # NOT VALID: the old list is a subset of the new one, so existing rows satisfy
    # it and no table scan runs. New and updated rows are checked at once.
    op.execute(
        f"ALTER TABLE research.us_candles_1m ADD CONSTRAINT {US_VENUE_CHECK} "
        f"CHECK (venue IN ({_quoted(US_VENUES_NEW)})) NOT VALID"
    )
    op.execute("ALTER TABLE research.us_candles_1m ALTER COLUMN value DROP NOT NULL")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM research.us_candles_1m
                WHERE venue = 'TOSS_INTEGRATED' LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot narrow venue: TOSS_INTEGRATED rows exist'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM research.us_candles_1m WHERE value IS NULL LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot restore NOT NULL: rows with NULL value exist'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$
        """
    )
    op.execute(
        f"ALTER TABLE research.us_candles_1m DROP CONSTRAINT IF EXISTS {US_VENUE_CHECK}"
    )
    op.execute(
        f"ALTER TABLE research.us_candles_1m ADD CONSTRAINT {US_VENUE_CHECK} "
        f"CHECK (venue IN ({_quoted(US_VENUES_OLD)})) NOT VALID"
    )
    op.execute("ALTER TABLE research.us_candles_1m ALTER COLUMN value SET NOT NULL")
