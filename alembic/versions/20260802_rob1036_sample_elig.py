"""ROB-1036 invalid-sample eligibility + cleanup binding (additive, append-only).

Revision ID: 20260802_rob1036_sample_elig
Revises: 20260728_rob1109_watch_intent
Create Date: 2026-08-02

Purely additive: three new tables in the existing ``review`` schema plus their
append-only triggers. No existing table, column, constraint, or row is touched,
and nothing here backfills a historical decision — a subject with no row is
``UNIDENTIFIABLE`` by the service contract, never ``INCLUDE``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260802_rob1036_sample_elig"
down_revision = "20260728_rob1109_watch_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256 = "^[0-9a-f]{64}$"
_APPEND_ONLY_TABLES = (
    "sample_eligibility_decisions",
    "invalid_sample_cleanup_bindings",
    "invalid_sample_cleanup_lifecycle_events",
)


def _create_reject_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION review.reject_invalid_sample_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'review.% is append-only; % rejected',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_append_only_triggers(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_rob1036_{table}_append_only "
        f"BEFORE UPDATE OR DELETE ON review.{table} FOR EACH ROW EXECUTE "
        "FUNCTION review.reject_invalid_sample_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER trg_rob1036_{table}_truncate_append_only "
        f"BEFORE TRUNCATE ON review.{table} FOR EACH STATEMENT EXECUTE "
        "FUNCTION review.reject_invalid_sample_mutation()"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    _create_reject_function()

    op.create_table(
        "sample_eligibility_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("supersedes_revision_no", sa.Integer(), nullable=True),
        sa.Column("forecast_outcome_observability", sa.Text(), nullable=False),
        sa.Column("calibration_eligibility", sa.Text(), nullable=False),
        sa.Column("trade_performance_eligibility", sa.Text(), nullable=False),
        sa.Column("operational_reliability_eligibility", sa.Text(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_kind",
            "subject_ref",
            "revision_no",
            name="uq_sample_eligibility_subject_revision",
        ),
        sa.CheckConstraint(
            "subject_kind IN ('forecast','trade_lifecycle')",
            name="ck_sample_eligibility_subject_kind",
        ),
        sa.CheckConstraint(
            "revision_no >= 1", name="ck_sample_eligibility_revision_no"
        ),
        sa.CheckConstraint(
            "(revision_no = 1 AND supersedes_revision_no IS NULL) OR "
            "(revision_no > 1 AND supersedes_revision_no = revision_no - 1)",
            name="ck_sample_eligibility_revision_chain",
        ),
        sa.CheckConstraint(
            "forecast_outcome_observability IN "
            "('observable','blocked_pending_audit_evidence','unidentifiable')",
            name="ck_sample_eligibility_observability",
        ),
        sa.CheckConstraint(
            "calibration_eligibility IN "
            "('calibration_include','calibration_exclude','calibration_unidentifiable')",
            name="ck_sample_eligibility_calibration",
        ),
        sa.CheckConstraint(
            "trade_performance_eligibility IN ('trade_performance_include',"
            "'trade_performance_exclude','trade_performance_unidentifiable')",
            name="ck_sample_eligibility_trade_performance",
        ),
        sa.CheckConstraint(
            "operational_reliability_eligibility IN "
            "('operational_include','operational_exclude','operational_unidentifiable')",
            name="ck_sample_eligibility_operational",
        ),
        sa.CheckConstraint(
            f"evidence_hash ~ '{_SHA256}'",
            name="ck_sample_eligibility_evidence_hash",
        ),
        sa.CheckConstraint(
            "btrim(contract_version) <> ''",
            name="ck_sample_eligibility_contract_version",
        ),
        sa.CheckConstraint(
            "btrim(decision_reason) <> ''",
            name="ck_sample_eligibility_decision_reason",
        ),
        schema="review",
    )
    op.create_index(
        "uq_sample_eligibility_supersedes",
        "sample_eligibility_decisions",
        ["subject_kind", "subject_ref", "supersedes_revision_no"],
        unique=True,
        schema="review",
        postgresql_where=sa.text("supersedes_revision_no IS NOT NULL"),
    )
    op.create_index(
        "ix_sample_eligibility_subject",
        "sample_eligibility_decisions",
        ["subject_kind", "subject_ref"],
        schema="review",
    )
    op.create_index(
        "ix_sample_eligibility_contract_version",
        "sample_eligibility_decisions",
        ["contract_version"],
        schema="review",
    )

    op.create_table(
        "invalid_sample_cleanup_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("forecast_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sample_ref", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=False),
        sa.Column("approval_hash", sa.Text(), nullable=False),
        sa.Column("approval_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approval_session_id", sa.Text(), nullable=False),
        sa.Column("mission_id", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("lifecycle_correlation_id", sa.Text(), nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_order_id", name="uq_invalid_sample_binding_client_order_id"
        ),
        sa.UniqueConstraint("binding_hash", name="uq_invalid_sample_binding_hash"),
        sa.CheckConstraint(
            "purpose = 'invalid_sample_cleanup'",
            name="ck_invalid_sample_binding_purpose",
        ),
        sa.CheckConstraint(
            f"binding_hash ~ '{_SHA256}'",
            name="ck_invalid_sample_binding_hash_format",
        ),
        sa.CheckConstraint(
            "btrim(mission_id) <> '' AND btrim(approval_id) <> '' "
            "AND btrim(approval_session_id) <> ''",
            name="ck_invalid_sample_binding_identities",
        ),
        schema="review",
    )
    op.create_index(
        "ix_invalid_sample_binding_forecast_id",
        "invalid_sample_cleanup_bindings",
        ["forecast_id"],
        schema="review",
    )
    op.create_index(
        "ix_invalid_sample_binding_correlation_id",
        "invalid_sample_cleanup_bindings",
        ["lifecycle_correlation_id"],
        schema="review",
    )

    op.create_table(
        "invalid_sample_cleanup_lifecycle_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("binding_hash", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("completion_status", sa.Text(), nullable=False),
        sa.Column("manual_review_reason", sa.Text(), nullable=True),
        sa.Column("fill_evidence", sa.Text(), nullable=False),
        sa.Column("position_effect_evidence", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "binding_hash",
            "event_kind",
            "evidence_hash",
            name="uq_invalid_sample_lifecycle_event_identity",
        ),
        sa.CheckConstraint(
            "event_kind IN ('post_fill_completion','post_fill_manual_review',"
            "'timeout_recovery_lookup')",
            name="ck_invalid_sample_lifecycle_event_kind",
        ),
        sa.CheckConstraint(
            "completion_status IN ('complete','manual_review')",
            name="ck_invalid_sample_lifecycle_completion_status",
        ),
        sa.CheckConstraint(
            "(completion_status = 'complete' AND manual_review_reason IS NULL) OR "
            "(completion_status = 'manual_review' AND manual_review_reason IS NOT NULL)",
            name="ck_invalid_sample_lifecycle_reason_pairing",
        ),
        sa.CheckConstraint(
            f"evidence_hash ~ '{_SHA256}'",
            name="ck_invalid_sample_lifecycle_evidence_hash",
        ),
        schema="review",
    )
    op.create_index(
        "ix_invalid_sample_lifecycle_binding_hash",
        "invalid_sample_cleanup_lifecycle_events",
        ["binding_hash"],
        schema="review",
    )
    op.create_index(
        "ix_invalid_sample_lifecycle_client_order_id",
        "invalid_sample_cleanup_lifecycle_events",
        ["client_order_id"],
        schema="review",
    )

    for table in _APPEND_ONLY_TABLES:
        _create_append_only_triggers(table)


def downgrade() -> None:
    for table in _APPEND_ONLY_TABLES:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob1036_{table}_truncate_append_only "
            f"ON review.{table}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_rob1036_{table}_append_only ON review.{table}"
        )
    op.drop_index(
        "ix_invalid_sample_lifecycle_client_order_id",
        table_name="invalid_sample_cleanup_lifecycle_events",
        schema="review",
    )
    op.drop_index(
        "ix_invalid_sample_lifecycle_binding_hash",
        table_name="invalid_sample_cleanup_lifecycle_events",
        schema="review",
    )
    op.drop_table("invalid_sample_cleanup_lifecycle_events", schema="review")
    op.drop_index(
        "ix_invalid_sample_binding_correlation_id",
        table_name="invalid_sample_cleanup_bindings",
        schema="review",
    )
    op.drop_index(
        "ix_invalid_sample_binding_forecast_id",
        table_name="invalid_sample_cleanup_bindings",
        schema="review",
    )
    op.drop_table("invalid_sample_cleanup_bindings", schema="review")
    op.drop_index(
        "ix_sample_eligibility_contract_version",
        table_name="sample_eligibility_decisions",
        schema="review",
    )
    op.drop_index(
        "ix_sample_eligibility_subject",
        table_name="sample_eligibility_decisions",
        schema="review",
    )
    op.drop_index(
        "uq_sample_eligibility_supersedes",
        table_name="sample_eligibility_decisions",
        schema="review",
    )
    op.drop_table("sample_eligibility_decisions", schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.reject_invalid_sample_mutation()")
