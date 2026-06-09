"""rob474 trade_retrospectives

Revision ID: 20260609_rob474
Revises: 20260609_rob455
Create Date: 2026-06-09
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260609_rob474"
down_revision: Union[str, Sequence[str], None] = "20260609_rob455"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_retrospectives",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("report_uuid", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(name="instrument_type", create_type=False),
            nullable=False,
        ),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column("strategy_key", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("plan_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("fill_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(20, 4), nullable=True),
        sa.Column("realized_pnl_currency", sa.Text(), nullable=True),
        sa.Column("realized_pnl_source", sa.Text(), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column(
            "fill_evidence_available",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("lesson", sa.Text(), nullable=True),
        sa.Column("next_strategy", sa.Text(), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by_profile", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["journal_id"], ["review.trade_journals.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "correlation_id", name="uq_trade_retrospectives_correlation_id"
        ),
        sa.CheckConstraint(
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
            name="ck_trade_retrospectives_account_mode",
        ),
        sa.CheckConstraint(
            "outcome IN ('filled','partially_filled','unfilled','rejected','cancelled')",
            name="ck_trade_retrospectives_outcome",
        ),
        sa.CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_trade_retrospectives_side",
        ),
        sa.CheckConstraint(
            "realized_pnl_currency IS NULL OR realized_pnl_currency IN ('KRW','USD')",
            name="ck_trade_retrospectives_currency",
        ),
        sa.CheckConstraint(
            "realized_pnl_source IS NULL OR "
            "realized_pnl_source IN ('caller_supplied','derived_from_journal')",
            name="ck_trade_retrospectives_pnl_source",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_correlation_id",
        "trade_retrospectives", ["correlation_id"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_journal_id",
        "trade_retrospectives", ["journal_id"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_strategy_key",
        "trade_retrospectives", ["strategy_key"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_symbol",
        "trade_retrospectives", ["symbol"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_report_uuid",
        "trade_retrospectives", ["report_uuid"], schema="review",
    )
    op.create_index(
        "ix_trade_retrospectives_account_mode_created",
        "trade_retrospectives", ["account_mode", "created_at"], schema="review",
    )


def downgrade() -> None:
    for ix in (
        "ix_trade_retrospectives_account_mode_created",
        "ix_trade_retrospectives_report_uuid",
        "ix_trade_retrospectives_symbol",
        "ix_trade_retrospectives_strategy_key",
        "ix_trade_retrospectives_journal_id",
        "ix_trade_retrospectives_correlation_id",
    ):
        op.drop_index(ix, "trade_retrospectives", schema="review")
    op.drop_table("trade_retrospectives", schema="review")
