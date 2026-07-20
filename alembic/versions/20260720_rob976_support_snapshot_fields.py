"""Persist support-proximity metrics on invest screener snapshots.

Revision ID: 20260720_rob976_support
Revises: 20260717_rob920_alpaca_canceled
Create Date: 2026-07-20 20:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260720_rob976_support"
down_revision = '20260721_rob954_terminalized_at'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("daily_turnover", sa.Numeric(30, 2), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("market_cap_source", sa.String(32), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("market_cap_snapshot_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("support_price", sa.Numeric(20, 6), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("support_kind", sa.String(255), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("support_strength", sa.String(20), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("dist_to_support_pct", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "invest_screener_snapshots",
        sa.Column("support_computed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_invest_screener_snapshots_market_support_distance",
        "invest_screener_snapshots",
        ["market", "snapshot_date", "dist_to_support_pct"],
        unique=False,
        postgresql_where=sa.text("dist_to_support_pct IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invest_screener_snapshots_market_support_distance",
        table_name="invest_screener_snapshots",
    )
    op.drop_column("invest_screener_snapshots", "support_computed_at")
    op.drop_column("invest_screener_snapshots", "dist_to_support_pct")
    op.drop_column("invest_screener_snapshots", "support_strength")
    op.drop_column("invest_screener_snapshots", "support_kind")
    op.drop_column("invest_screener_snapshots", "support_price")
    op.drop_column("invest_screener_snapshots", "market_cap_snapshot_date")
    op.drop_column("invest_screener_snapshots", "market_cap_source")
    op.drop_column("invest_screener_snapshots", "market_cap")
    op.drop_column("invest_screener_snapshots", "daily_turnover")
