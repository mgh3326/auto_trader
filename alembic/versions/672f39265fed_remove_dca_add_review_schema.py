"""Remove legacy DCA tables, add review schema and tables.

Revision ID: 672f39265fed
Revises: 86961c84a0ce
Create Date: 2026-03-17 10:36:52.860827
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "672f39265fed"
down_revision: str | Sequence[str] | None = "86961c84a0ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Existing instrument_type enum — reuse, do not create
instrument_type_enum = sa.Enum(
    "equity_kr", "equity_us", "crypto", "forex", "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Drop legacy DCA tables (0 rows, no model, no runtime references)
    # ------------------------------------------------------------------
    op.drop_index(
        op.f("ix_dca_plan_steps_order_id"), table_name="dca_plan_steps"
    )
    op.drop_index(
        op.f("ix_dca_plan_steps_plan_id"), table_name="dca_plan_steps"
    )
    op.drop_table("dca_plan_steps")

    op.drop_index(op.f("ix_dca_plans_symbol"), table_name="dca_plans")
    op.drop_index(op.f("ix_dca_plans_user_status"), table_name="dca_plans")
    op.drop_table("dca_plans")

    op.execute("DROP TYPE IF EXISTS dca_step_status")
    op.execute("DROP TYPE IF EXISTS dca_plan_status")

    # ------------------------------------------------------------------
    # 2. Create review schema
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS review")

    # ------------------------------------------------------------------
    # 3. review.trades
    # ------------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("total_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column(
            "fee", sa.Numeric(20, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "currency", sa.Text(), nullable=False, server_default="KRW"
        ),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "account", "order_id", name="uq_review_trades_account_order"
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="review_trades_side"
        ),
        sa.CheckConstraint(
            "currency IN ('KRW','USD')", name="review_trades_currency"
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_trades_trade_date",
        "trades",
        ["trade_date"],
        schema="review",
    )
    op.create_index(
        "ix_review_trades_symbol", "trades", ["symbol"], schema="review"
    )

    # ------------------------------------------------------------------
    # 4. review.trade_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "trade_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("rsi_14", sa.Numeric(6, 2), nullable=True),
        sa.Column("rsi_7", sa.Numeric(6, 2), nullable=True),
        sa.Column("ema_20", sa.Numeric(20, 4), nullable=True),
        sa.Column("ema_200", sa.Numeric(20, 4), nullable=True),
        sa.Column("macd", sa.Numeric(20, 4), nullable=True),
        sa.Column("macd_signal", sa.Numeric(20, 4), nullable=True),
        sa.Column("adx", sa.Numeric(6, 2), nullable=True),
        sa.Column("stoch_rsi_k", sa.Numeric(6, 2), nullable=True),
        sa.Column("volume_ratio", sa.Numeric(10, 2), nullable=True),
        sa.Column("fear_greed", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="CASCADE",
            name="fk_review_trade_snapshots_trade_id",
        ),
        sa.UniqueConstraint(
            "trade_id", name="uq_review_trade_snapshots_trade_id"
        ),
        schema="review",
    )

    # ------------------------------------------------------------------
    # 5. review.trade_reviews
    # ------------------------------------------------------------------
    op.create_table(
        "trade_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("review_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("price_at_review", sa.Numeric(20, 4), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "review_type",
            sa.Text(),
            nullable=False,
            server_default="daily",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["review.trades.id"],
            ondelete="CASCADE",
            name="fk_review_trade_reviews_trade_id",
        ),
        sa.CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="review_trade_reviews_verdict",
        ),
        sa.CheckConstraint(
            "review_type IN ('daily','weekly','monthly','manual')",
            name="review_trade_reviews_review_type",
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_trade_reviews_trade_type",
        "trade_reviews",
        ["trade_id", "review_type"],
        schema="review",
    )
    op.create_index(
        "ix_review_trade_reviews_review_date",
        "trade_reviews",
        ["review_date"],
        schema="review",
    )

    # ------------------------------------------------------------------
    # 6. review.pending_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "pending_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_date", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type_enum, nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_price", sa.Numeric(20, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("gap_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("days_pending", sa.Integer(), nullable=True),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column(
            "resolved_as",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "side IN ('buy','sell')", name="review_pending_side"
        ),
        sa.CheckConstraint(
            "resolved_as IN ('pending','filled','cancelled','expired')",
            name="review_pending_resolved_as",
        ),
        schema="review",
    )
    op.create_index(
        "ix_review_pending_resolved_date",
        "pending_snapshots",
        ["resolved_as", "snapshot_date"],
        schema="review",
    )
    op.create_index(
        "ix_review_pending_account_order_date",
        "pending_snapshots",
        ["account", "order_id", "snapshot_date"],
        schema="review",
    )


def downgrade() -> None:
    # Drop review tables (reverse order of creation)
    op.drop_index(
        "ix_review_pending_account_order_date",
        table_name="pending_snapshots",
        schema="review",
    )
    op.drop_index(
        "ix_review_pending_resolved_date",
        table_name="pending_snapshots",
        schema="review",
    )
    op.drop_table("pending_snapshots", schema="review")

    op.drop_index(
        "ix_review_trade_reviews_review_date",
        table_name="trade_reviews",
        schema="review",
    )
    op.drop_index(
        "ix_review_trade_reviews_trade_type",
        table_name="trade_reviews",
        schema="review",
    )
    op.drop_table("trade_reviews", schema="review")

    op.drop_table("trade_snapshots", schema="review")

    op.drop_index(
        "ix_review_trades_symbol", table_name="trades", schema="review"
    )
    op.drop_index(
        "ix_review_trades_trade_date", table_name="trades", schema="review"
    )
    op.drop_table("trades", schema="review")

    op.execute("DROP SCHEMA IF EXISTS review")

    # NOTE: DCA tables are NOT restored on downgrade — they were unused.
