"""Add append-only review.screener_pick_log (prospective fanout picks).

Revision ID: 20260823_screener_pick_log
Revises: 20260821_w5_callback_inbox
Create Date: 2026-08-23

Additive DDL only. No existing table, column, constraint, or row is touched.
This migration is included in the PR; operators apply it separately with
``alembic upgrade head``. Nothing in this repo auto-applies it, and the
writer is default-off behind ``SCREENER_PICK_LOG_ENABLED``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260823_screener_pick_log"
down_revision: str | Sequence[str] | None = "20260821_w5_callback_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "screener_pick_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("recorded_at_kst", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("decision_price_text", sa.Text(), nullable=True),
        sa.Column("source_sort_by", sa.Text(), nullable=True),
        sa.Column("source_sort_order", sa.Text(), nullable=True),
        sa.Column("source_limit", sa.Integer(), nullable=True),
        sa.Column("source_preset", sa.Text(), nullable=True),
        sa.Column("fanout_version", sa.Text(), nullable=False),
        sa.Column("fanout_code_sha256", sa.Text(), nullable=False),
        sa.Column(
            "source_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="market",
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0",
            name="source_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="symbol_nonempty",
        ),
        sa.CheckConstraint(
            "rank IS NULL OR rank >= 1",
            name="rank_positive",
        ),
        sa.CheckConstraint(
            "source_limit IS NULL OR source_limit >= 1",
            name="limit_positive",
        ),
        sa.CheckConstraint(
            "decision_price_text IS NULL OR "
            "decision_price_text ~ '^-?[0-9]+(\\.[0-9]+)?$'",
            name="price_decimal_text",
        ),
        sa.CheckConstraint(
            "fanout_code_sha256 ~ '^[0-9a-f]{64}$'",
            name="code_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_params) = 'object'",
            name="source_params_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "call_id",
            "source",
            "symbol",
            name="uq_screener_pick_log_call_source_symbol",
        ),
        schema="review",
    )
    op.create_index(
        "ix_screener_pick_log_recorded_at",
        "screener_pick_log",
        ["recorded_at"],
        schema="review",
    )
    op.create_index(
        "ix_screener_pick_log_market_source_recorded",
        "screener_pick_log",
        ["market", "source", "recorded_at"],
        schema="review",
    )
    op.execute(
        """
        CREATE FUNCTION review.reject_screener_pick_log_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'review.screener_pick_log is append-only; % rejected',
                TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_screener_pick_log_append_only
        BEFORE UPDATE OR DELETE ON review.screener_pick_log
        FOR EACH ROW EXECUTE FUNCTION review.reject_screener_pick_log_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_screener_pick_log_append_only "
        "ON review.screener_pick_log"
    )
    op.execute("DROP FUNCTION IF EXISTS review.reject_screener_pick_log_mutation()")
    op.drop_index(
        "ix_screener_pick_log_market_source_recorded",
        table_name="screener_pick_log",
        schema="review",
    )
    op.drop_index(
        "ix_screener_pick_log_recorded_at",
        table_name="screener_pick_log",
        schema="review",
    )
    op.drop_table("screener_pick_log", schema="review")
