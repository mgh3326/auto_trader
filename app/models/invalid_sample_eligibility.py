"""ROB-1036 — append-only eligibility / cleanup-binding tables.

Three tables, all append-only at the database edge (BEFORE UPDATE/DELETE/TRUNCATE
triggers created by the migration):

``review.sample_eligibility_decisions``
    One row per revision of the four-domain eligibility decision.  A correction
    is a superseding revision, never an overwrite.
``review.invalid_sample_cleanup_bindings``
    Immutable purpose ↔ sample ↔ approval ↔ mission ↔ broker-lifecycle binding.
``review.invalid_sample_cleanup_lifecycle_events``
    Append-only post-fill evidence trail for a bound cleanup leg.

Every write goes through
``app.services.invalid_sample_eligibility.service.InvalidSampleEligibilityService``.
Direct SQL writes are forbidden (guarded by
``tests/services/invalid_sample_eligibility/test_static_boundaries.py``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_SHA256_HEX = "^[0-9a-f]{64}$"


class SampleEligibilityDecision(Base):
    """Append-only revision of the four independent validity domains.

    The four domain columns are stored separately and are never reduced to one
    ``is_valid`` column: a forecast whose outcome stays observable can still be
    excluded from calibration, and vice versa.
    """

    __tablename__ = "sample_eligibility_decisions"
    __table_args__ = (
        UniqueConstraint(
            "subject_kind",
            "subject_ref",
            "revision_no",
            name="uq_sample_eligibility_subject_revision",
        ),
        # No two revisions may supersede the same predecessor: that is a branch.
        Index(
            "uq_sample_eligibility_supersedes",
            "subject_kind",
            "subject_ref",
            "supersedes_revision_no",
            unique=True,
            postgresql_where=text("supersedes_revision_no IS NOT NULL"),
        ),
        CheckConstraint(
            "subject_kind IN ('forecast','trade_lifecycle')",
            name="ck_sample_eligibility_subject_kind",
        ),
        CheckConstraint("revision_no >= 1", name="ck_sample_eligibility_revision_no"),
        # Revision 1 opens the chain; every later revision supersedes exactly its
        # predecessor. This makes a gap, a branch, and a cycle unrepresentable.
        CheckConstraint(
            "(revision_no = 1 AND supersedes_revision_no IS NULL) OR "
            "(revision_no > 1 AND supersedes_revision_no = revision_no - 1)",
            name="ck_sample_eligibility_revision_chain",
        ),
        CheckConstraint(
            "forecast_outcome_observability IN "
            "('observable','blocked_pending_audit_evidence','unidentifiable')",
            name="ck_sample_eligibility_observability",
        ),
        CheckConstraint(
            "calibration_eligibility IN "
            "('calibration_include','calibration_exclude','calibration_unidentifiable')",
            name="ck_sample_eligibility_calibration",
        ),
        CheckConstraint(
            "trade_performance_eligibility IN ('trade_performance_include',"
            "'trade_performance_exclude','trade_performance_unidentifiable')",
            name="ck_sample_eligibility_trade_performance",
        ),
        CheckConstraint(
            "operational_reliability_eligibility IN "
            "('operational_include','operational_exclude','operational_unidentifiable')",
            name="ck_sample_eligibility_operational",
        ),
        CheckConstraint(
            f"evidence_hash ~ '{_SHA256_HEX}'",
            name="ck_sample_eligibility_evidence_hash",
        ),
        CheckConstraint(
            "btrim(contract_version) <> ''",
            name="ck_sample_eligibility_contract_version",
        ),
        CheckConstraint(
            "btrim(decision_reason) <> ''",
            name="ck_sample_eligibility_decision_reason",
        ),
        Index(
            "ix_sample_eligibility_subject",
            "subject_kind",
            "subject_ref",
        ),
        Index("ix_sample_eligibility_contract_version", "contract_version"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)

    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_revision_no: Mapped[int | None] = mapped_column(Integer)

    # Four independent domains — deliberately four columns, not one flag.
    forecast_outcome_observability: Mapped[str] = mapped_column(Text, nullable=False)
    calibration_eligibility: Mapped[str] = mapped_column(Text, nullable=False)
    trade_performance_eligibility: Mapped[str] = mapped_column(Text, nullable=False)
    operational_reliability_eligibility: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    decision_reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    evidence_hash: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class InvalidSampleCleanupBinding(Base):
    """Immutable binding of a cleanup leg to its approval and mission."""

    __tablename__ = "invalid_sample_cleanup_bindings"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id", name="uq_invalid_sample_binding_client_order_id"
        ),
        UniqueConstraint("binding_hash", name="uq_invalid_sample_binding_hash"),
        CheckConstraint(
            "purpose = 'invalid_sample_cleanup'",
            name="ck_invalid_sample_binding_purpose",
        ),
        CheckConstraint(
            f"binding_hash ~ '{_SHA256_HEX}'",
            name="ck_invalid_sample_binding_hash_format",
        ),
        CheckConstraint(
            "btrim(mission_id) <> '' AND btrim(approval_id) <> '' "
            "AND btrim(approval_session_id) <> ''",
            name="ck_invalid_sample_binding_identities",
        ),
        Index("ix_invalid_sample_binding_forecast_id", "forecast_id"),
        Index(
            "ix_invalid_sample_binding_correlation_id",
            "lifecycle_correlation_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)

    forecast_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    sample_ref: Mapped[str] = mapped_column(Text, nullable=False)

    approval_id: Mapped[str] = mapped_column(Text, nullable=False)
    approval_hash: Mapped[str] = mapped_column(Text, nullable=False)
    approval_expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    approval_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    mission_id: Mapped[str] = mapped_column(Text, nullable=False)

    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)

    binding_hash: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class InvalidSampleCleanupLifecycleEvent(Base):
    """Append-only post-fill evidence trail for a bound cleanup leg."""

    __tablename__ = "invalid_sample_cleanup_lifecycle_events"
    __table_args__ = (
        # Replaying the same evidence is a no-op, not a second event.
        UniqueConstraint(
            "binding_hash",
            "event_kind",
            "evidence_hash",
            name="uq_invalid_sample_lifecycle_event_identity",
        ),
        CheckConstraint(
            "event_kind IN ('post_fill_completion','post_fill_manual_review',"
            "'timeout_recovery_lookup')",
            name="ck_invalid_sample_lifecycle_event_kind",
        ),
        CheckConstraint(
            "completion_status IN ('complete','manual_review')",
            name="ck_invalid_sample_lifecycle_completion_status",
        ),
        # A complete event carries no refusal reason; a manual-review event must.
        CheckConstraint(
            "(completion_status = 'complete' AND manual_review_reason IS NULL) OR "
            "(completion_status = 'manual_review' AND manual_review_reason IS NOT NULL)",
            name="ck_invalid_sample_lifecycle_reason_pairing",
        ),
        CheckConstraint(
            f"evidence_hash ~ '{_SHA256_HEX}'",
            name="ck_invalid_sample_lifecycle_evidence_hash",
        ),
        Index("ix_invalid_sample_lifecycle_binding_hash", "binding_hash"),
        Index("ix_invalid_sample_lifecycle_client_order_id", "client_order_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    binding_hash: Mapped[str] = mapped_column(Text, nullable=False)
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)

    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    completion_status: Mapped[str] = mapped_column(Text, nullable=False)
    manual_review_reason: Mapped[str | None] = mapped_column(Text)

    fill_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    position_effect_evidence: Mapped[str] = mapped_column(Text, nullable=False)

    evidence: Mapped[dict | None] = mapped_column(JSONB)
    evidence_hash: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "InvalidSampleCleanupBinding",
    "InvalidSampleCleanupLifecycleEvent",
    "SampleEligibilityDecision",
]
