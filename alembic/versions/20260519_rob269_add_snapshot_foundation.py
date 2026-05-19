# alembic/versions/20260519_rob269_add_snapshot_foundation.py
"""rob-269 phase 1: snapshot foundation (runs/snapshots/bundles/items)

Revision ID: 20260519_rob269_p1
Revises: 20260519_rob265_drop_legacy
Create Date: 2026-05-19

Adds 4 immutable tables under ``review`` schema. All additive. Append-only
invariant is enforced at the service layer (Task 4) — no DB trigger in v1.

See: docs/superpowers/plans/2026-05-19-rob-269-pre-plan.md (Decisions 1, 3).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260519_rob269_p1"
down_revision: str | None = "20260519_rob265_drop_legacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb_default(literal: str) -> sa.sql.elements.TextClause:
    return sa.text(f"'{literal}'::jsonb")


_ACCOUNT_SCOPE_CHECK = (
    "account_scope IS NULL OR account_scope IN "
    "('kis_live','kis_mock','alpaca_paper','upbit_live')"
)
_MARKET_CHECK = "market IN ('kr','us','crypto')"


def upgrade() -> None:
    # ----------------------------------------------------------------
    # review.investment_snapshot_runs
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("run_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "policy_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("refresh_reason", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.UniqueConstraint("run_uuid", name="uq_investment_snapshot_runs_run_uuid"),
        sa.CheckConstraint(
            "purpose IN ('report_generation','scheduled_refresh',"
            "'manual_refresh','reviewer_requested')",
            name="ck_investment_snapshot_runs_purpose",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_runs_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_runs_account_scope",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_investment_snapshot_runs_status",
        ),
        sa.CheckConstraint(
            "requested_by IN ('hermes','user','scheduler','claude_code','reviewer')",
            name="ck_investment_snapshot_runs_requested_by",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_runs_purpose_market_started",
        "investment_snapshot_runs",
        ["purpose", "market", sa.text("started_at DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_runs_status_started",
        "investment_snapshot_runs",
        ["status", sa.text("started_at DESC")],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshots
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("snapshot_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("review.investment_snapshot_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("source_table", sa.Text(), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "source_timestamps_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "coverage_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "errors_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("freshness_status", sa.Text(), nullable=False),
        sa.Column("canonical_payload_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "snapshot_uuid", name="uq_investment_snapshots_snapshot_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_investment_snapshots_idempotency_key"
        ),
        sa.UniqueConstraint(
            "canonical_payload_hash",
            "snapshot_kind",
            "market",
            "account_scope",
            name="uq_investment_snapshots_canonical_dedup",
        ),
        sa.CheckConstraint(
            "snapshot_kind IN ('portfolio','market','news','symbol',"
            "'candidate_universe','browser_probe','invest_page',"
            "'journal','watch_context','naver_remote_debug',"
            "'toss_remote_debug','llm_input_frozen')",
            name="ck_investment_snapshots_snapshot_kind",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshots_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshots_account_scope",
        ),
        sa.CheckConstraint(
            "source_kind IN ('kis_mcp','auto_trader_mcp','invest_api',"
            "'naver_remote_debug','toss_remote_debug','combined',"
            "'news_ingestor','manual','domain_ref')",
            name="ck_investment_snapshots_source_kind",
        ),
        sa.CheckConstraint(
            "freshness_status IN ('fresh','soft_stale','hard_stale',"
            "'partial','unavailable')",
            name="ck_investment_snapshots_freshness_status",
        ),
        sa.CheckConstraint(
            "(source_table IS NULL AND source_id IS NULL AND source_uri IS NULL) "
            "OR (source_table IS NOT NULL AND source_id IS NOT NULL "
            "AND source_uri IS NOT NULL)",
            name="ck_investment_snapshots_source_ref_triple",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_kind_market_symbol_as_of",
        "investment_snapshots",
        ["snapshot_kind", "market", "symbol", sa.text("as_of DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_source_uri",
        "investment_snapshots",
        ["source_uri"],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshots_run_id",
        "investment_snapshots",
        ["run_id"],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshot_bundles
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_bundles",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("bundle_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "policy_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "coverage_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column(
            "freshness_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_jsonb_default("{}"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "bundle_uuid", name="uq_investment_snapshot_bundles_bundle_uuid"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_investment_snapshot_bundles_idempotency_key",
        ),
        sa.CheckConstraint(_MARKET_CHECK, name="ck_investment_snapshot_bundles_market"),
        sa.CheckConstraint(
            _ACCOUNT_SCOPE_CHECK,
            name="ck_investment_snapshot_bundles_account_scope",
        ),
        sa.CheckConstraint(
            "status IN ('complete','partial','stale_fallback','failed')",
            name="ck_investment_snapshot_bundles_status",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundles_purpose_market_account_asof",
        "investment_snapshot_bundles",
        ["purpose", "market", "account_scope", sa.text("as_of DESC")],
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundles_status_created",
        "investment_snapshot_bundles",
        ["status", sa.text("created_at DESC")],
        schema="review",
    )

    # ----------------------------------------------------------------
    # review.investment_snapshot_bundle_items
    # ----------------------------------------------------------------
    op.create_table(
        "investment_snapshot_bundle_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "bundle_id",
            sa.BigInteger(),
            sa.ForeignKey("review.investment_snapshot_bundles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("review.investment_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "bundle_id",
            "snapshot_id",
            name="uq_investment_snapshot_bundle_items_bundle_snapshot",
        ),
        sa.CheckConstraint(
            "role IN ('required','optional','fallback','conflict_evidence')",
            name="ck_investment_snapshot_bundle_items_role",
        ),
        schema="review",
    )
    op.create_index(
        "ix_investment_snapshot_bundle_items_snapshot",
        "investment_snapshot_bundle_items",
        ["snapshot_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_table("investment_snapshot_bundle_items", schema="review")
    op.drop_table("investment_snapshot_bundles", schema="review")
    op.drop_table("investment_snapshots", schema="review")
    op.drop_table("investment_snapshot_runs", schema="review")
