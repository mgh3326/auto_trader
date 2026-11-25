"""hk 6510: US intraday movers rows in the momentum snapshot table.

Revision ID: 20261125_hk6510_us_movers
Revises: 20261125_hk6508_fnguide
Create Date: 2026-11-25

Additive, plus two CHECK replacements on one small observation table.

* ``invest_momentum_event_snapshots.market`` accepts ``us`` (was ``kr`` only) and
  ``source`` accepts ``alpaca_screener`` / ``kis_overseas_ranking`` (was
  ``naver_stock`` only). KR rows and their values are untouched.
* Three nullable columns carry what a US row must state: ``asset_type``,
  ``first_received_at`` and ``field_reasons`` (the closed reason of every field
  the source does not provide). A KR insert leaves all three NULL.
* ``ix_..._market_date_symbol`` backs ``first_captures`` for one market's trading
  day.

🔴 The CHECKs are replaced by **definition text**, not by name: the models carry
``ck_%(table_name)s_%(constraint_name)s``, so a CHECK created from that metadata is
stored double-prefixed and, past 63 bytes, truncated with a deterministic hash.
No single literal name exists in every database, and the two snapshot CHECKs
truncate to nearly the same prefix. ``pg_get_constraintdef`` does name them
reliably. The new constraints use the short names the model and the validate
revision refer to.

The two snapshot CHECKs are added ``NOT VALID`` so the ADD does not scan the
existing log while holding ACCESS EXCLUSIVE; the follow-up revision
``20261125_hk6510_us_movers_val`` runs ``VALIDATE CONSTRAINT`` (same pattern as
hk 6322). No new grant: the table's ``at_app`` grants are unchanged (hard rule 18).

Downgrade refuses when ``us`` snapshot rows or ``source_unavailable`` run rows
exist: they violate the vocabulary the downgrade restores, and discarding
observations is not a downgrade.
"""

# expand-contract: contract-step 6510
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261125_hk6510_us_movers"
down_revision: str | Sequence[str] | None = "20261125_hk6508_fnguide"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invest_momentum_event_snapshots"
RUNS_TABLE = "invest_momentum_event_runs"
MARKET_CHECK = "ck_invest_momentum_event_snapshots_market"
SOURCE_CHECK = "ck_invest_momentum_event_snapshots_source"
RUNS_CHECK = "ck_invest_momentum_event_runs_status_reason"
INDEX = "ix_invest_momentum_event_snapshots_market_date_symbol"

#: ``pg_get_constraintdef`` fragment that finds each CHECK regardless of how the
#: naming convention spelled it. One marker per CHECK in its table.
MARKET_MARKER = "market"
SOURCE_MARKER = "naver_stock"
RUNS_MARKER = "no_rows"

_RUNS_VOCABULARY = (
    "'http_4xx', 'http_5xx', 'timeout', 'transport', "
    "'bad_response', 'unparseable', 'unexpected'"
)


def _drop_check(table: str, marker: str) -> None:
    """Drop the CHECK whose definition contains ``marker`` (naming-convention safe)."""
    op.execute(
        f"""
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = '{table}'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%{marker}%'
            LOOP
                EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END
        $$
        """
    )


def _runs_check_sql(reasons: str) -> str:
    return (
        "CHECK ("
        "(status = 'ok' AND reason IS NULL AND row_count > 0)"
        " OR (status = 'empty' AND reason = 'no_rows' AND row_count = 0)"
        " OR (status = 'error' AND row_count = 0 AND reason IN ("
        f"{reasons})))"
    )


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("asset_type", sa.String(length=20), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(TABLE, sa.Column("field_reasons", postgresql.JSONB(), nullable=True))
    op.create_index(INDEX, TABLE, ["market", "trading_date", "symbol", "snapshot_at"])
    _drop_check(TABLE, MARKET_MARKER)
    _drop_check(TABLE, SOURCE_MARKER)
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {MARKET_CHECK} "
        "CHECK (market IN ('kr', 'us')) NOT VALID"
    )
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SOURCE_CHECK} "
        "CHECK (source IN ('naver_stock', 'alpaca_screener', "
        "'kis_overseas_ranking')) NOT VALID"
    )
    # The US run can report ``source_unavailable`` (no reader / no credentials), so
    # the closed reason vocabulary of the status row grows by that one value.
    _drop_check(RUNS_TABLE, RUNS_MARKER)
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {RUNS_TABLE} ADD CONSTRAINT {RUNS_CHECK} "
        + _runs_check_sql(f"{_RUNS_VOCABULARY}, 'source_unavailable'")
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.{TABLE} WHERE market = 'us') THEN
                RAISE EXCEPTION
                    '{TABLE} has us rows; downgrade would discard US observations';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.{RUNS_TABLE}
                WHERE reason = 'source_unavailable'
            ) THEN
                RAISE EXCEPTION
                    '{RUNS_TABLE} has source_unavailable rows; downgrade would discard them';
            END IF;
        END
        $$
        """
    )
    op.drop_index(INDEX, table_name=TABLE)
    _drop_check(RUNS_TABLE, RUNS_MARKER)
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {RUNS_TABLE} ADD CONSTRAINT {RUNS_CHECK} "
        + _runs_check_sql(_RUNS_VOCABULARY)
    )
    _drop_check(TABLE, MARKET_MARKER)
    _drop_check(TABLE, SOURCE_MARKER)
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {MARKET_CHECK} CHECK (market = 'kr')"
    )
    # expand-contract: contract-step 6510
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SOURCE_CHECK} "
        "CHECK (source = 'naver_stock')"
    )
    op.drop_column(TABLE, "field_reasons")
    op.drop_column(TABLE, "first_received_at")
    op.drop_column(TABLE, "asset_type")
