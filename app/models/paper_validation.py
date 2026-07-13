"""Append-only ROB-848 paper-validation audit models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv
from sqlalchemy.sql import func

from app.models.base import Base

_HASH_CHECK = " AND ".join(
    f"{name} ~ '^[0-9a-f]{{64}}$'"
    for name in (
        "experiment_hash",
        "cohort_hash",
        "strategy_hash",
        "config_hash",
        "policy_hash",
        "input_hash",
    )
)


class PaperValidationStateTransition(Base):
    __tablename__ = "paper_validation_state_transitions"
    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            "sequence",
            name="uq_paper_validation_transition_sequence",
        ),
        UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_paper_validation_transition_idempotency",
        ),
        CheckConstraint(
            "actor_role IN ('researcher','reviewer','operator','system')",
            name=conv("ck_paper_validation_transition_actor_role"),
        ),
        CheckConstraint(
            f"experiment_hash = experiment_id AND {_HASH_CHECK}",
            name=conv("ck_paper_validation_transition_hashes"),
        ),
        CheckConstraint(
            "(sequence = 1 AND prior_state IS NULL AND new_state = 'draft') OR "
            "(sequence > 1 AND ("
            "(prior_state = 'draft' AND new_state = 'offline_eligible') OR "
            "(prior_state = 'offline_eligible' AND new_state = 'shadow_soak') OR "
            "(prior_state = 'shadow_soak' AND new_state = 'paper_active') OR "
            "(prior_state = 'paper_active' AND new_state = 'promotion_eligible') OR "
            "(prior_state = 'promotion_eligible' AND new_state IN "
            "('promoted','rejected','aborted'))))",
            name=conv("ck_paper_validation_transition_graph"),
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_ids) = 'array'",
            name=conv("ck_paper_validation_transition_evidence_array"),
        ),
        Index(
            "ix_paper_validation_transition_history",
            "validation_id",
            "sequence",
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    validation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_paper_validation_transition_experiment",
        ),
        nullable=False,
    )
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyHypothesisDraft(Base):
    __tablename__ = "strategy_hypothesis_drafts"
    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_strategy_hypothesis_draft_idempotency",
        ),
        CheckConstraint(
            "author_role = 'researcher'",
            name=conv("ck_strategy_hypothesis_draft_author_role"),
        ),
        CheckConstraint(
            _HASH_CHECK,
            name=conv("ck_strategy_hypothesis_draft_hashes"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    validation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_strategy_hypothesis_draft_experiment",
        ),
        nullable=False,
    )
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    author_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_role: Mapped[str] = mapped_column(String(16), nullable=False)
    mechanism: Mapped[str] = mapped_column(Text, nullable=False)
    universe: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    horizon: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    exit_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    invalidation_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    data_requirements: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    expected_cost_hurdle: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False
    )
    turnover_bound: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    risk_bound: Mapped[Decimal] = mapped_column(Numeric(24, 12), nullable=False)
    cited_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperValidationPostmortemReview(Base):
    __tablename__ = "paper_validation_postmortem_reviews"
    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            "idempotency_key",
            name="uq_paper_validation_review_idempotency",
        ),
        CheckConstraint(
            "evaluator_role = 'reviewer'",
            name=conv("ck_paper_validation_review_evaluator_role"),
        ),
        CheckConstraint(
            _HASH_CHECK,
            name=conv("ck_paper_validation_review_hashes"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    validation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_paper_validation_review_experiment",
        ),
        nullable=False,
    )
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluator_role: Mapped[str] = mapped_column(String(16), nullable=False)
    review_text: Mapped[str] = mapped_column(Text, nullable=False)
    cited_evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "PaperValidationPostmortemReview",
    "PaperValidationStateTransition",
    "StrategyHypothesisDraft",
]
