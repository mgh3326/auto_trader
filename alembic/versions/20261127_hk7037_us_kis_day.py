"""hk 7037: KIS day-market venue KIS_DAY in research.us_candles_1m (expand step).

Revision ID: 20261127_hk7037_us_kis_day
Revises: 20261127_hk6991_intent_v3
Create Date: 2026-11-27

Additive. ``public.*_candles_1m`` are untouched, and no column changes.

fillwire (hk 6347) emits KIS 주간거래 (day-market) bars with EXCD ``BAY``/``BAQ``/
``BAA`` (TR HHDFS76950200). Those codes are not listing exchanges, so they are not
written as NASD/NYSE/AMEX. The producer maps them to the one venue ``KIS_DAY``
(the execution venue of the KIS day market), and the HTTP ingest refuses the raw
codes with ``unknown_venue``.

1. ``research.us_candles_1m`` venue CHECK also admits ``KIS_DAY``. The constraint
   is replaced NOT VALID: every existing row satisfies the old list, which is a
   subset of the new one, so no scan runs under ACCESS EXCLUSIVE. The follow-up
   revision ``20261127_hk7037_us_kis_day_val`` runs VALIDATE CONSTRAINT.

Downgrade refuses while any row has venue ``KIS_DAY`` (the narrowed CHECK would fail
and those rows are first-receipt history). Nothing is dropped.
"""

# expand-contract: contract-step 7037
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261127_hk7037_us_kis_day"
down_revision: str | Sequence[str] | None = "20261127_hk6991_intent_v3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

US_VENUE_CHECK = "ck_research_us_candles_1m_venue"
US_VENUES_OLD = ("NASD", "NYSE", "AMEX", "TOSS_INTEGRATED")
US_VENUES_NEW = (*US_VENUES_OLD, "KIS_DAY")


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


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM research.us_candles_1m
                WHERE venue = 'KIS_DAY' LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot narrow venue: KIS_DAY rows exist'
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
