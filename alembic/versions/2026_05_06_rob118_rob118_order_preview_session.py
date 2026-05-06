"""rob118_order_preview_session

Revision ID: 2026_05_06_rob118
Revises: f7891bf9789f
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026_05_06_rob118"
down_revision = "f7891bf9789f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_preview_session",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("preview_uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),  # portfolio_action | candidate | research_run
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("research_session_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),  # equity_kr | equity_us | crypto
        sa.Column("venue", sa.String(32), nullable=False),  # live | paper | crypto_live | ...
        sa.Column("side", sa.String(8), nullable=False),  # buy | sell
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        # created | preview_passed | preview_failed | submitted | submit_failed | canceled
        sa.Column("dry_run_payload", sa.JSON, nullable=True),
        sa.Column("dry_run_error", sa.JSON, nullable=True),
        sa.Column("approval_token", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_preview_session_user", "order_preview_session", ["user_id", "created_at"])

    op.create_table(
        "order_preview_leg",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger, sa.ForeignKey("order_preview_session.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leg_index", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False, server_default="limit"),
        sa.Column("estimated_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("estimated_fee", sa.Numeric(20, 8), nullable=True),
        sa.Column("expected_pnl", sa.Numeric(20, 8), nullable=True),
        sa.Column("dry_run_status", sa.String(32), nullable=True),  # passed | failed | skipped
        sa.Column("dry_run_error", sa.JSON, nullable=True),
        sa.UniqueConstraint("session_id", "leg_index", name="uq_preview_leg_session_idx"),
    )

    op.create_table(
        "order_execution_request",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger, sa.ForeignKey("order_preview_session.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leg_id", sa.BigInteger, sa.ForeignKey("order_preview_leg.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),  # submitted | rejected | failed
        sa.Column("error_payload", sa.JSON, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("order_execution_request")
    op.drop_table("order_preview_leg")
    op.drop_index("ix_order_preview_session_user", table_name="order_preview_session")
    op.drop_table("order_preview_session")
