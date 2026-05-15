"""add analysis report action center tables

Revision ID: 20260515_rob257
Revises: 20260513_rob225
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260515_rob257"
down_revision: str | None = "20260513_rob225"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("created_by_profile", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=True),
        sa.Column("data_freshness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("safety_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','published','superseded','expired')",
            name="analysis_reports_status_allowed",
        ),
        sa.UniqueConstraint("report_uuid", name="uq_analysis_reports_report_uuid"),
        sa.UniqueConstraint("idempotency_key", name="uq_analysis_reports_idempotency_key"),
        schema="review",
    )
    op.create_index(
        "ix_analysis_reports_market_created",
        "analysis_reports",
        ["market", "created_at"],
        schema="review",
    )
    op.create_index(
        "ix_analysis_reports_status_created",
        "analysis_reports",
        ["status", "created_at"],
        schema="review",
    )

    op.create_table(
        "analysis_stage_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("stage_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("freshness_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("normalized_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok','stale','unavailable','error')",
            name="analysis_stage_status_allowed",
        ),
        sa.ForeignKeyConstraint(["report_id"], ["review.analysis_reports.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("report_id", "stage_key", "source", name="uq_analysis_stage_report_stage_source"),
        schema="review",
    )
    op.create_index(
        "ix_analysis_stage_results_report",
        "analysis_stage_results",
        ["report_id"],
        schema="review",
    )

    op.create_table(
        "analysis_order_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("quantity_pct", sa.Numeric(10, 6), nullable=True),
        sa.Column("limit_price", sa.Numeric(20, 4), nullable=True),
        sa.Column("notional", sa.Numeric(20, 4), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 4), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("risk_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocking_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approval_status", sa.Text(), nullable=False),
        sa.Column("approval_type", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("policy_id", sa.Text(), nullable=True),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("execution_state", sa.Text(), nullable=False),
        sa.Column("linked_trade_journal_id", sa.BigInteger(), nullable=True),
        sa.Column("linked_order_ledger_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("side IN ('buy','sell')", name="analysis_candidates_side_allowed"),
        sa.CheckConstraint(
            "approval_status IN ('awaiting_approval','approved','rejected','expired')",
            name="analysis_candidates_approval_status_allowed",
        ),
        sa.CheckConstraint(
            "execution_state IN ('not_submitted','blocked','submitted_elsewhere')",
            name="analysis_candidates_execution_state_allowed",
        ),
        sa.ForeignKeyConstraint(["report_id"], ["review.analysis_reports.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("candidate_uuid", name="uq_analysis_candidates_candidate_uuid"),
        sa.UniqueConstraint("idempotency_key", name="uq_analysis_candidates_idempotency_key"),
        schema="review",
    )
    op.create_index(
        "ix_analysis_candidates_symbol_market",
        "analysis_order_candidates",
        ["symbol", "market"],
        schema="review",
    )
    op.create_index(
        "ix_analysis_candidates_approval",
        "analysis_order_candidates",
        ["approval_status", "created_at"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_candidates_approval", table_name="analysis_order_candidates", schema="review")
    op.drop_index("ix_analysis_candidates_symbol_market", table_name="analysis_order_candidates", schema="review")
    op.drop_table("analysis_order_candidates", schema="review")
    op.drop_index("ix_analysis_stage_results_report", table_name="analysis_stage_results", schema="review")
    op.drop_table("analysis_stage_results", schema="review")
    op.drop_index("ix_analysis_reports_status_created", table_name="analysis_reports", schema="review")
    op.drop_index("ix_analysis_reports_market_created", table_name="analysis_reports", schema="review")
    op.drop_table("analysis_reports", schema="review")
