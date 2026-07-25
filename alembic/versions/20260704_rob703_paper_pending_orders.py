"""ROB-703 paper_pending_orders

Revision ID: 20260704_rob703
Revises: 20260702_rob653
Create Date: 2026-07-04 12:32:00.000000

Add ``paper.paper_pending_orders`` for the resting-limit paper sim.
Stores resting buy/sell limit orders until ``PaperLimitOrderService.reconcile_pending_orders``
decides the live market crossed the limit; ``reserved_krw`` is held against
the account cash balance until the order fills or is cancelled.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_rob703"
down_revision: Union[str, Sequence[str], None] = "20260702_rob653"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_pending_orders",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column(
            "order_type",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'limit'"),
        ),
        sa.Column("limit_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column(
            "reserved_krw",
            sa.Numeric(precision=20, scale=4),
            nullable=False,
            server_default=sa.text("'0'"),
        ),
        sa.Column(
            "status",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("fill_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("paper_trade_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "placed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="paper_pending_orders_side"
        ),
        sa.CheckConstraint(
            "order_type IN ('limit')", name="paper_pending_orders_order_type"
        ),
        sa.CheckConstraint(
            "status IN ('pending','filled','cancelled')",
            name="paper_pending_orders_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper.paper_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_trade_id"],
            ["paper.paper_trades.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="paper",
    )
    op.create_index(
        "ix_paper_pending_orders_account_id",
        "paper_pending_orders",
        ["account_id"],
        schema="paper",
    )
    op.create_index(
        "ix_paper_pending_orders_status",
        "paper_pending_orders",
        ["status"],
        schema="paper",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_pending_orders_status",
        table_name="paper_pending_orders",
        schema="paper",
    )
    op.drop_index(
        "ix_paper_pending_orders_account_id",
        table_name="paper_pending_orders",
        schema="paper",
    )
    op.drop_table("paper_pending_orders", schema="paper")
