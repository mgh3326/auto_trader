"""add investor flow snapshots

Revision ID: 9f1a2b3c4d5e
Revises: 82309c07b8a2
Create Date: 2026-05-11 09:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f1a2b3c4d5e"
down_revision: str | None = "82309c07b8a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investor_flow_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("foreign_net", sa.BigInteger(), nullable=True),
        sa.Column("institution_net", sa.BigInteger(), nullable=True),
        sa.Column("individual_net", sa.BigInteger(), nullable=True),
        sa.Column("foreign_net_buy_rank", sa.Integer(), nullable=True),
        sa.Column("foreign_net_sell_rank", sa.Integer(), nullable=True),
        sa.Column("institution_net_buy_rank", sa.Integer(), nullable=True),
        sa.Column("institution_net_sell_rank", sa.Integer(), nullable=True),
        sa.Column("double_buy", sa.Boolean(), nullable=False),
        sa.Column("double_sell", sa.Boolean(), nullable=False),
        sa.Column("foreign_consecutive_buy_days", sa.Integer(), nullable=True),
        sa.Column("foreign_consecutive_sell_days", sa.Integer(), nullable=True),
        sa.Column("institution_consecutive_buy_days", sa.Integer(), nullable=True),
        sa.Column("institution_consecutive_sell_days", sa.Integer(), nullable=True),
        sa.Column("individual_consecutive_buy_days", sa.Integer(), nullable=True),
        sa.Column("individual_consecutive_sell_days", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("market IN ('kr')", name="ck_investor_flow_snapshots_market"),
        sa.CheckConstraint(
            "source IN ('naver_finance', 'kis', 'manual')",
            name="ck_investor_flow_snapshots_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_investor_flow_snapshots_market_symbol_date_source",
        ),
    )
    op.create_index(
        "ix_investor_flow_snapshots_market_symbol_date",
        "investor_flow_snapshots",
        ["market", "symbol", "snapshot_date"],
    )
    op.create_index(
        "ix_investor_flow_snapshots_market_foreign_rank",
        "investor_flow_snapshots",
        ["market", "foreign_net_buy_rank"],
        postgresql_where=sa.text("foreign_net_buy_rank IS NOT NULL"),
    )
    op.create_index(
        "ix_investor_flow_snapshots_market_institution_rank",
        "investor_flow_snapshots",
        ["market", "institution_net_buy_rank"],
        postgresql_where=sa.text("institution_net_buy_rank IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investor_flow_snapshots_market_institution_rank",
        table_name="investor_flow_snapshots",
        postgresql_where=sa.text("institution_net_buy_rank IS NOT NULL"),
    )
    op.drop_index(
        "ix_investor_flow_snapshots_market_foreign_rank",
        table_name="investor_flow_snapshots",
        postgresql_where=sa.text("foreign_net_buy_rank IS NOT NULL"),
    )
    op.drop_index(
        "ix_investor_flow_snapshots_market_symbol_date",
        table_name="investor_flow_snapshots",
    )
    op.drop_table("investor_flow_snapshots")
