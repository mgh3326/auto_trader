"""Add market fields to investor flow snapshots for ROB-575.

Revision ID: 20260615_rob575
Revises: 20260615_rob568_us_fx_pnl
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260615_rob575"
down_revision = "20260615_rob568_us_fx_pnl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("close", sa.Numeric(20, 6), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("volume", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("foreign_holding_shares", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "investor_flow_snapshots",
        sa.Column("foreign_holding_rate", sa.Numeric(10, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investor_flow_snapshots", "foreign_holding_rate")
    op.drop_column("investor_flow_snapshots", "foreign_holding_shares")
    op.drop_column("investor_flow_snapshots", "volume")
    op.drop_column("investor_flow_snapshots", "change_rate")
    op.drop_column("investor_flow_snapshots", "close")
