"""hk 6510: US intraday movers rows in the momentum snapshot table.

Revision ID: 20261125_hk6510_us_movers
Revises: 20261125_hk6322_us_venue_val
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

The two CHECKs are added ``NOT VALID`` so the ADD does not scan the existing log
while holding ACCESS EXCLUSIVE; the follow-up revision
``20261125_hk6510_us_movers_val`` runs ``VALIDATE CONSTRAINT`` (same pattern as
hk 6322). No new grant: the table's ``at_app`` grants are unchanged (hard rule 18).

Downgrade refuses when ``us`` rows exist: they violate the restored ``kr``-only
CHECK, and silently deleting observations is not a downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261125_hk6510_us_movers"
down_revision: str | Sequence[str] | None = "20261125_hk6322_us_venue_val"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invest_momentum_event_snapshots"
MARKET_CHECK = "ck_invest_momentum_event_snapshots_market"
SOURCE_CHECK = "ck_invest_momentum_event_snapshots_source"
RUNS_TABLE = "invest_momentum_event_runs"
RUNS_CHECK = "ck_invest_momentum_event_runs_status_reason"
INDEX = "ix_invest_momentum_event_snapshots_market_date_symbol"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("asset_type", sa.String(length=20), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("first_received_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(TABLE, sa.Column("field_reasons", postgresql.JSONB(), nullable=True))
    op.drop_constraint(MARKET_CHECK, TABLE, type_="check")
    op.drop_constraint(SOURCE_CHECK, TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {MARKET_CHECK} "
        "CHECK (market IN ('kr', 'us')) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SOURCE_CHECK} "
        "CHECK (source IN ('naver_stock', 'alpaca_screener', "
        "'kis_overseas_ranking')) NOT VALID"
    )
    op.create_index(
        INDEX,
        TABLE,
        ["market", "trading_date", "symbol", "snapshot_at"],
    )
    # The US run can report ``source_unavailable`` (no reader / no credentials), so
    # the closed reason vocabulary of the status row grows by that one value. The
    # runs table holds one day of a manual job, so the ADD scans it directly.
    op.drop_constraint(RUNS_CHECK, RUNS_TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {RUNS_TABLE} ADD CONSTRAINT {RUNS_CHECK} CHECK ("
        "(status = 'ok' AND reason IS NULL AND row_count > 0)"
        " OR (status = 'empty' AND reason = 'no_rows' AND row_count = 0)"
        " OR (status = 'error' AND row_count = 0 AND reason IN ("
        "'http_4xx', 'http_5xx', 'timeout', 'transport', "
        "'bad_response', 'unparseable', 'unexpected', 'source_unavailable')))"
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
        END
        $$
        """
    )
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_constraint(RUNS_CHECK, RUNS_TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {RUNS_TABLE} ADD CONSTRAINT {RUNS_CHECK} CHECK ("
        "(status = 'ok' AND reason IS NULL AND row_count > 0)"
        " OR (status = 'empty' AND reason = 'no_rows' AND row_count = 0)"
        " OR (status = 'error' AND row_count = 0 AND reason IN ("
        "'http_4xx', 'http_5xx', 'timeout', 'transport', "
        "'bad_response', 'unparseable', 'unexpected')))"
    )
    op.drop_constraint(MARKET_CHECK, TABLE, type_="check")
    op.drop_constraint(SOURCE_CHECK, TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {MARKET_CHECK} CHECK (market = 'kr')"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {SOURCE_CHECK} "
        "CHECK (source = 'naver_stock')"
    )
    op.drop_column(TABLE, "field_reasons")
    op.drop_column(TABLE, "first_received_at")
    op.drop_column(TABLE, "asset_type")
