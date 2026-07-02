"""ROB-650 resolvable forecast ledger

Revision ID: 20260702_rob650
Revises: 20260702_rob641
Create Date: 2026-07-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260702_rob650"
down_revision: str | None = "20260702_rob641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_forecasts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "forecast_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("artifact_uuid", sa.Text(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("report_uuid", sa.Text(), nullable=True),
        sa.Column("report_item_uuid", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("session_label", sa.Text(), nullable=True),
        sa.Column("model_label", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(name="instrument_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "forecast_target",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("horizon", sa.Text(), nullable=True),
        sa.Column("probability", sa.Numeric(5, 4), nullable=False),
        sa.Column("probability_range_low", sa.Numeric(5, 4), nullable=True),
        sa.Column("probability_range_high", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("contrary_evidence", sa.Text(), nullable=True),
        sa.Column("resolution_source", sa.Text(), nullable=True),
        sa.Column("forecast_start_date", sa.Date(), nullable=True),
        sa.Column("review_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("outcome", sa.Boolean(), nullable=True),
        sa.Column("observed_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("brier_score", sa.Numeric(6, 5), nullable=True),
        sa.Column(
            "resolution_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("forecast_id", name="uq_trade_forecasts_forecast_id"),
        sa.CheckConstraint(
            "status IN ('open','closed')",
            name="ck_trade_forecasts_status",
        ),
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="ck_trade_forecasts_probability",
        ),
        sa.CheckConstraint(
            "(probability_range_low IS NULL AND probability_range_high IS NULL) OR "
            "(probability_range_low IS NOT NULL "
            "AND probability_range_high IS NOT NULL "
            "AND probability_range_low <= probability_range_high "
            "AND probability >= probability_range_low "
            "AND probability <= probability_range_high)",
            name="ck_trade_forecasts_probability_range",
        ),
        sa.CheckConstraint(
            "brier_score IS NULL OR (brier_score >= 0 AND brier_score <= 1)",
            name="ck_trade_forecasts_brier_score",
        ),
        schema="review",
    )
    op.create_index(
        "ix_trade_forecasts_status_review_date",
        "trade_forecasts",
        ["status", "review_date"],
        schema="review",
    )
    op.create_index(
        "ix_trade_forecasts_symbol",
        "trade_forecasts",
        ["symbol"],
        schema="review",
    )
    op.create_index(
        "ix_trade_forecasts_created_by",
        "trade_forecasts",
        ["created_by"],
        schema="review",
    )
    op.create_index(
        "ix_trade_forecasts_correlation_id",
        "trade_forecasts",
        ["correlation_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_forecasts_correlation_id",
        table_name="trade_forecasts",
        schema="review",
    )
    op.drop_index(
        "ix_trade_forecasts_created_by",
        table_name="trade_forecasts",
        schema="review",
    )
    op.drop_index(
        "ix_trade_forecasts_symbol",
        table_name="trade_forecasts",
        schema="review",
    )
    op.drop_index(
        "ix_trade_forecasts_status_review_date",
        table_name="trade_forecasts",
        schema="review",
    )
    op.drop_table("trade_forecasts", schema="review")
