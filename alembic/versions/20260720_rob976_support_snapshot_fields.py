"""Persist support-proximity metrics on invest screener snapshots.

Revision ID: 20260720_rob976_support
Revises: 20260717_rob920_alpaca_canceled
Create Date: 2026-07-20 20:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260720_rob976_support"
down_revision = '20260721_rob954_terminalized_at'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "invest_screener_snapshots"
_INDEX = "ix_invest_screener_snapshots_market_support_distance"

# IF NOT EXISTS / IF EXISTS mirror the rob954 pattern (see
# 20260721_rob954_terminalized_at.py): the migration acceptance test creates
# current Base.metadata first (columns already present), stamps an older
# revision, then upgrades to head, so these statements must be idempotent.
# Production upgrades still add the genuinely absent columns normally.
_COLUMNS = (
    ("daily_turnover", "NUMERIC(30, 2)"),
    ("market_cap", "NUMERIC(30, 2)"),
    ("market_cap_source", "VARCHAR(32)"),
    ("market_cap_snapshot_date", "DATE"),
    ("support_price", "NUMERIC(20, 6)"),
    ("support_kind", "VARCHAR(255)"),
    ("support_strength", "VARCHAR(20)"),
    ("dist_to_support_pct", "NUMERIC(10, 4)"),
    ("support_computed_at", "TIMESTAMP WITH TIME ZONE"),
)


def upgrade() -> None:
    for column, coltype in _COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {coltype}"
        )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        f"ON {_TABLE} (market, snapshot_date, dist_to_support_pct) "
        "WHERE dist_to_support_pct IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    for column, _ in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
