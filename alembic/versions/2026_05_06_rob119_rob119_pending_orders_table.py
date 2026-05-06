"""rob119_pending_orders_table

Revision ID: 2026_05_06_rob119
Revises: 2026_05_06_rob118
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026_05_06_rob119"
down_revision = "2026_05_06_rob118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("broker_order_id", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("venue", "broker_order_id", name="uq_pending_order_venue_id"),
    )
    op.create_index("ix_pending_orders_symbol", "pending_orders", ["symbol"])


def downgrade() -> None:
    op.drop_table("pending_orders")
