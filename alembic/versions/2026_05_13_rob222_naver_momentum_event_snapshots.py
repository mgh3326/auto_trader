"""ROB-222 add Naver momentum/theme event snapshot tables.

Revision ID: 20260513_rob222
Revises: 20260513_rob211k3
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260513_rob222"
down_revision = "20260513_rob211k3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invest_momentum_event_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="naver_stock"),
        sa.Column("surface", sa.String(length=80), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False, server_default="kr"),
        sa.Column("trade_type", sa.String(length=16), nullable=True),
        sa.Column("market_type", sa.String(length=16), nullable=True),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("change_amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("trade_value", sa.Numeric(30, 2), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("market = 'kr'", name="ck_invest_momentum_event_snapshots_market"),
        sa.CheckConstraint("source = 'naver_stock'", name="ck_invest_momentum_event_snapshots_source"),
        sa.UniqueConstraint(
            "surface",
            "snapshot_at",
            "trade_type",
            "market_type",
            "order_type",
            "symbol",
            name="uq_invest_momentum_event_snapshots_surface_params_symbol_at",
        ),
    )
    op.create_index(
        "ix_invest_momentum_event_snapshots_date_order_rank",
        "invest_momentum_event_snapshots",
        ["trading_date", "order_type", "rank"],
    )
    op.create_index(
        "ix_invest_momentum_event_snapshots_symbol_date",
        "invest_momentum_event_snapshots",
        ["symbol", "trading_date"],
    )
    op.create_index(
        "ix_invest_momentum_event_snapshots_surface_params_at",
        "invest_momentum_event_snapshots",
        ["surface", "trade_type", "market_type", "order_type", "snapshot_at"],
    )

    op.create_table(
        "invest_theme_event_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="naver_stock"),
        sa.Column("surface", sa.String(length=80), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False, server_default="kr"),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("source_event_key", sa.String(length=160), nullable=False),
        sa.Column("naver_theme_no", sa.String(length=40), nullable=True),
        sa.Column("naver_upjong_code", sa.String(length=40), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_type", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("market_type", sa.String(length=16), nullable=True),
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("trade_value", sa.Numeric(30, 2), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column("stock_count", sa.Integer(), nullable=True),
        sa.Column("leader_symbols", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("market = 'kr'", name="ck_invest_theme_event_snapshots_market"),
        sa.CheckConstraint("source = 'naver_stock'", name="ck_invest_theme_event_snapshots_source"),
        sa.CheckConstraint("event_kind IN ('theme', 'upjong')", name="ck_invest_theme_event_snapshots_kind"),
        sa.UniqueConstraint("snapshot_at", "source_event_key", name="uq_invest_theme_event_snapshots_at_key"),
    )
    op.create_index(
        "ix_invest_theme_event_snapshots_date_kind_sort_rank",
        "invest_theme_event_snapshots",
        ["trading_date", "event_kind", "sort_type", "rank"],
    )
    op.create_index(
        "ix_invest_theme_event_snapshots_kind_key_at",
        "invest_theme_event_snapshots",
        ["event_kind", "source_event_key", "snapshot_at"],
    )

    op.create_table(
        "invest_theme_event_snapshot_stocks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("theme_snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("order_type", sa.String(length=32), nullable=True),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("change_amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("trade_value", sa.Numeric(30, 2), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["theme_snapshot_id"],
            ["invest_theme_event_snapshots.id"],
            name="fk_invest_theme_event_snapshot_stocks_theme_snapshot_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "theme_snapshot_id",
            "order_type",
            "symbol",
            name="uq_invest_theme_event_snapshot_stocks_parent_order_symbol",
        ),
    )
    op.create_index("ix_invest_theme_event_snapshot_stocks_symbol", "invest_theme_event_snapshot_stocks", ["symbol"])
    op.create_index(
        "ix_invest_theme_event_snapshot_stocks_parent_rank",
        "invest_theme_event_snapshot_stocks",
        ["theme_snapshot_id", "rank"],
    )


def downgrade() -> None:
    op.drop_index("ix_invest_theme_event_snapshot_stocks_parent_rank", table_name="invest_theme_event_snapshot_stocks")
    op.drop_index("ix_invest_theme_event_snapshot_stocks_symbol", table_name="invest_theme_event_snapshot_stocks")
    op.drop_table("invest_theme_event_snapshot_stocks")
    op.drop_index("ix_invest_theme_event_snapshots_kind_key_at", table_name="invest_theme_event_snapshots")
    op.drop_index("ix_invest_theme_event_snapshots_date_kind_sort_rank", table_name="invest_theme_event_snapshots")
    op.drop_table("invest_theme_event_snapshots")
    op.drop_index("ix_invest_momentum_event_snapshots_surface_params_at", table_name="invest_momentum_event_snapshots")
    op.drop_index("ix_invest_momentum_event_snapshots_symbol_date", table_name="invest_momentum_event_snapshots")
    op.drop_index("ix_invest_momentum_event_snapshots_date_order_rank", table_name="invest_momentum_event_snapshots")
    op.drop_table("invest_momentum_event_snapshots")
