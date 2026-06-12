"""ROB-534 add Toss symbol master fields and valuation source

Revision ID: 20260612_rob534
Revises: 20260611_rob516_rob512_merge
Create Date: 2026-06-12 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260612_rob534"
down_revision = "20260611_rob516_rob512_merge"
branch_labels = None
depends_on = None


def _add_common_columns(table: str, *, kr: bool) -> None:
    op.add_column(table, sa.Column("security_type", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("is_common_share", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("listing_status", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("list_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("delist_date", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("shares_outstanding", sa.Numeric(30, 0), nullable=True))
    op.add_column(table, sa.Column("leverage_factor", sa.Numeric(12, 6), nullable=True))
    if kr:
        op.add_column(table, sa.Column("krx_trading_suspended", sa.Boolean(), nullable=True))
        op.add_column(table, sa.Column("nxt_trading_suspended", sa.Boolean(), nullable=True))
    op.add_column(table, sa.Column("isin", sa.String(length=20), nullable=True))
    op.add_column(table, sa.Column("toss_master_updated_at", sa.TIMESTAMP(timezone=True), nullable=True))


def _drop_common_columns(table: str, *, kr: bool) -> None:
    op.drop_column(table, "toss_master_updated_at")
    op.drop_column(table, "isin")
    if kr:
        op.drop_column(table, "nxt_trading_suspended")
        op.drop_column(table, "krx_trading_suspended")
    op.drop_column(table, "leverage_factor")
    op.drop_column(table, "shares_outstanding")
    op.drop_column(table, "delist_date")
    op.drop_column(table, "list_date")
    op.drop_column(table, "listing_status")
    op.drop_column(table, "is_common_share")
    op.drop_column(table, "security_type")


def upgrade() -> None:
    _add_common_columns("kr_symbol_universe", kr=True)
    _add_common_columns("us_symbol_universe", kr=False)
    op.drop_constraint("ck_market_valuation_snapshots_source", "market_valuation_snapshots", type_="check")
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo', 'toss_openapi')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_market_valuation_snapshots_source", "market_valuation_snapshots", type_="check")
    op.create_check_constraint(
        "ck_market_valuation_snapshots_source",
        "market_valuation_snapshots",
        "source IN ('naver_finance', 'yahoo')",
    )
    _drop_common_columns("us_symbol_universe", kr=False)
    _drop_common_columns("kr_symbol_universe", kr=True)
