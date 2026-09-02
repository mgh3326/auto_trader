"""ROB-1340 append-only Kiwoom send-authority evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KiwoomAuthorityAttempt(Base):
    """Enumeration row committed before any advisory-lock request."""

    __tablename__ = "kiwoom_authority_attempts"
    __table_args__ = (
        UniqueConstraint("authority_attempt_id", name="uq_kiwoom_authority_attempt_id"),
        CheckConstraint(
            "contract_version = 'rob1340.v1'",
            name="contract_version_rob1340_v1",
        ),
        CheckConstraint("lane_id = 'kr.kiwoom.mock'", name="lane_kr_kiwoom_mock"),
        CheckConstraint("key_count > 0", name="key_count_positive"),
        CheckConstraint(
            "baseline_matching_rows = 0", name="baseline_matching_rows_zero"
        ),
        CheckConstraint(
            "owner_binding_digest ~ '^[0-9a-f]{64}$'",
            name="owner_binding_digest_sha256",
        ),
        CheckConstraint(
            "keyset_digest ~ '^[0-9a-f]{64}$'", name="keyset_digest_sha256"
        ),
        Index("ix_kiwoom_authority_attempt_cycle", "cycle_id", "id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    authority_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    lane_id: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_binding_digest: Mapped[str] = mapped_column(Text, nullable=False)
    keyset_digest: Mapped[str] = mapped_column(Text, nullable=False)
    key_count: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_matching_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class KiwoomAuthorityCessationReceipt(Base):
    """One terminal row per attempt; qualifying kinds prove authority cessation."""

    __tablename__ = "kiwoom_authority_cessation_receipts"
    __table_args__ = (
        UniqueConstraint(
            "authority_attempt_id", name="uq_kiwoom_authority_receipt_attempt"
        ),
        UniqueConstraint("receipt_digest", name="uq_kiwoom_authority_receipt_digest"),
        CheckConstraint(
            "contract_version = 'rob1340.v1'",
            name="contract_rob1340_v1",
        ),
        CheckConstraint("lane_id = 'kr.kiwoom.mock'", name="lane_kr_kiwoom_mock"),
        CheckConstraint("key_count > 0", name="key_count_positive"),
        CheckConstraint(
            "acquired_key_count >= 0 AND acquired_key_count <= key_count",
            name="acquired_count_bounded",
        ),
        CheckConstraint(
            "unlock_true_count >= 0 AND unlock_true_count <= acquired_key_count",
            name="unlock_count_bounded",
        ),
        CheckConstraint(
            "owner_binding_digest ~ '^[0-9a-f]{64}$'",
            name="owner_digest_sha256",
        ),
        CheckConstraint(
            "keyset_digest ~ '^[0-9a-f]{64}$'", name="keyset_digest_sha256"
        ),
        CheckConstraint(
            "receipt_digest ~ '^[0-9a-f]{64}$'", name="receipt_digest_sha256"
        ),
        CheckConstraint(
            "terminal_state IN "
            "('NO_KEY_ACQUIRED_PROVEN','CESSATION_RECEIPT_COMMITTED',"
            "'UNRESOLVED_HOLD')",
            name="terminal_state",
        ),
        CheckConstraint(
            "kind IN "
            "('no_key_acquired_proven','advisory_unlock',"
            "'backend_termination','unresolved_hold')",
            name="cessation_kind",
        ),
        CheckConstraint(
            "CASE kind "
            "WHEN 'no_key_acquired_proven' THEN "
            "terminal_state = 'NO_KEY_ACQUIRED_PROVEN' "
            "AND acquired_key_count = 0 AND in_flight_unknown = false "
            "AND ((lock_statement_dispatched = false "
            "AND lock_definite_false = false) "
            "OR (lock_statement_dispatched = true "
            "AND lock_definite_false = true)) "
            "AND unlock_true_count = 0 AND post_release_matching_rows = 0 "
            "AND termination_returned_exact_true IS NULL "
            "AND observer_pid_absent IS NULL "
            "WHEN 'advisory_unlock' THEN "
            "terminal_state = 'CESSATION_RECEIPT_COMMITTED' "
            "AND lock_statement_dispatched = true "
            "AND lock_definite_false = false "
            "AND acquired_key_count > 0 AND in_flight_unknown = false "
            "AND unlock_true_count = acquired_key_count "
            "AND post_release_matching_rows = 0 "
            "AND termination_returned_exact_true IS NULL "
            "AND observer_pid_absent IS NULL "
            "WHEN 'backend_termination' THEN "
            "terminal_state = 'CESSATION_RECEIPT_COMMITTED' "
            "AND lock_statement_dispatched = true "
            "AND lock_definite_false = false "
            "AND (acquired_key_count > 0 OR in_flight_unknown = true) "
            "AND post_release_matching_rows IS NULL "
            "AND termination_returned_exact_true = true "
            "AND observer_pid_absent = true "
            "WHEN 'unresolved_hold' THEN "
            "terminal_state = 'UNRESOLVED_HOLD' "
            "AND lock_statement_dispatched = true "
            "AND lock_definite_false = false "
            "AND (acquired_key_count > 0 OR in_flight_unknown = true) "
            "AND post_release_matching_rows IS NULL "
            "AND termination_returned_exact_true IS NULL "
            "AND observer_pid_absent IS NULL "
            "END",
            name="kind_exact_proof",
        ),
        Index("ix_kiwoom_authority_receipt_cycle", "cycle_id", "id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    authority_attempt_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "review.kiwoom_authority_attempts.authority_attempt_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    lane_id: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_row_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_binding_digest: Mapped[str] = mapped_column(Text, nullable=False)
    keyset_digest: Mapped[str] = mapped_column(Text, nullable=False)
    key_count: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_state: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    lock_statement_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lock_definite_false: Mapped[bool] = mapped_column(Boolean, nullable=False)
    acquired_key_count: Mapped[int] = mapped_column(Integer, nullable=False)
    in_flight_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unlock_true_count: Mapped[int] = mapped_column(Integer, nullable=False)
    post_release_matching_rows: Mapped[int | None] = mapped_column(Integer)
    termination_returned_exact_true: Mapped[bool | None] = mapped_column(Boolean)
    observer_pid_absent: Mapped[bool | None] = mapped_column(Boolean)
    receipt_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["KiwoomAuthorityAttempt", "KiwoomAuthorityCessationReceipt"]
