"""rob315 scalping daily review loop tables

Review layer on top of ``scalp_trade_analytics`` (ROB-315 Phase 1):
``scalping_daily_reviews`` (one row per review_date/product/account_scope/
session_tag, with the rollup metrics + operator judgment) and
``scalping_review_actions`` (discrete follow-ups). Additive only; raw
analytics + order-lifecycle tables are untouched.

Revision ID: 20260525_rob315
Revises: 20260525_rob313
Create Date: 2026-05-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260525_rob315"
down_revision: str | Sequence[str] | None = "20260525_rob313"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scalping_daily_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("review_date", sa.Date(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column(
            "account_scope",
            sa.Text(),
            server_default="binance_demo",
            nullable=False,
        ),
        sa.Column("session_tag", sa.Text(), server_default="", nullable=False),
        sa.Column("trade_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("win_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("loss_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("anomaly_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gross_pnl_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("net_pnl_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("net_return_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_slippage_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_mae_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_mfe_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("avg_holding_seconds", sa.Integer(), nullable=True),
        sa.Column("exit_reason_counts", postgresql.JSONB(), nullable=True),
        sa.Column("observation", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("improvement", sa.Text(), nullable=True),
        sa.Column("next_run_plan", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), server_default="review", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source_payload", postgresql.JSONB(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_date",
            "product",
            "account_scope",
            "session_tag",
            name="uq_scalping_daily_review_key",
        ),
        sa.CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="scalping_daily_review_product",
        ),
        sa.CheckConstraint(
            "account_scope = 'binance_demo'",
            name="scalping_daily_review_account_scope",
        ),
        sa.CheckConstraint(
            "decision IN ('review','keep','adjust','pause','disable')",
            name="scalping_daily_review_decision",
        ),
        sa.CheckConstraint(
            "status IN ('draft','reviewed','locked')",
            name="scalping_daily_review_status",
        ),
    )
    op.create_index(
        "ix_scalping_daily_review_date", "scalping_daily_reviews", ["review_date"]
    )
    op.create_index(
        "ix_scalping_daily_review_product", "scalping_daily_reviews", ["product"]
    )

    op.create_table(
        "scalping_review_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("review_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("target_component", sa.Text(), nullable=True),
        sa.Column("proposed_change", sa.Text(), nullable=True),
        sa.Column("expected_effect", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["scalping_daily_reviews.id"],
            name="fk_scalping_review_action_review_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "action_type IN ('parameter_change','investigate','pause','resume',"
            "'add_guard','data_quality','no_change')",
            name="scalping_review_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('open','applied','skipped','superseded')",
            name="scalping_review_action_status",
        ),
    )
    op.create_index(
        "ix_scalping_review_action_review_id",
        "scalping_review_actions",
        ["review_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scalping_review_action_review_id", table_name="scalping_review_actions"
    )
    op.drop_table("scalping_review_actions")
    op.drop_index(
        "ix_scalping_daily_review_product", table_name="scalping_daily_reviews"
    )
    op.drop_index("ix_scalping_daily_review_date", table_name="scalping_daily_reviews")
    op.drop_table("scalping_daily_reviews")
