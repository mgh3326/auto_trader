"""add trade_journals table

Revision ID: d15c37b0d793
Revises: 2d4e6f8a0b1c
Create Date: 2026-03-29 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d15c37b0d793"
down_revision: str | Sequence[str] | None = "2d4e6f8a0b1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

instrument_type_enum = sa.Enum(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "trade_journals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False, server_default="buy"),
        sa.Column("entry_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(20, 4), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("hold_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("exit_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="SET NULL",
            name="fk_trade_journals_trade_id_trades",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','closed','stopped','expired')",
            name="trade_journals_status_allowed",
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')",
            name="trade_journals_side",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_symbol_status",
        "trade_journals",
        ["symbol", "status"],
        schema="review",
    )
    op.create_index(
        "ix_trade_journals_created",
        "trade_journals",
        ["created_at"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_journals_created",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_index(
        "ix_trade_journals_symbol_status",
        table_name="trade_journals",
        schema="review",
    )
    op.drop_table("trade_journals", schema="review")
