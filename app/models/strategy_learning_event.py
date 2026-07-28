"""Append-only ROB-1115 strategy learning-memory events."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv
from sqlalchemy.sql import func

from app.models.base import Base

LEARNING_EVENT_STAGES: tuple[str, ...] = (
    "discovery",
    "offline",
    "sealed_oos",
    "shadow",
    "paper",
    "live",
    "ops",
)
LEARNING_EVENT_VERDICTS: tuple[str, ...] = (
    "promote",
    "iterate",
    "retire",
    "inconclusive",
    "retry_same_identity",
)
LEARNING_EVENT_FAILURE_CLASSES: tuple[str, ...] = (
    "data_quality",
    "insufficient_evidence",
    "no_signal",
    "gross_edge",
    "cost_turnover",
    "robustness",
    "risk",
    "execution_gap",
    "operational",
)

_STAGES_SQL = ",".join(f"'{value}'" for value in LEARNING_EVENT_STAGES)
_VERDICTS_SQL = ",".join(f"'{value}'" for value in LEARNING_EVENT_VERDICTS)
_FAILURE_CLASSES_SQL = ",".join(
    f"'{value}'" for value in LEARNING_EVENT_FAILURE_CLASSES
)
_SHA256 = "^[0-9a-f]{64}$"


class ResearchStrategyLearningEvent(Base):
    """One immutable learning result.

    ``experiment_id`` is a nullable FK by design: production had no registered
    experiments when ROB-1115 was specified, so unregistered historical tracks
    must be able to append memory immediately. A non-null value is still
    referentially bound to the ROB-846 registry.

    ``failure_fingerprint`` and ``learning_payload`` store the same closed typed
    canonical AST used by ROB-846. They are decoded only at the typed service
    boundary; PostgreSQL JSONB round-trips cannot rewrite finite floats.
    """

    __tablename__ = "strategy_learning_events"
    __table_args__ = (
        UniqueConstraint(
            "memory_event_id",
            name="uq_strategy_learning_event_memory_event_id",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_strategy_learning_event_idempotency_key",
        ),
        CheckConstraint(
            f"memory_event_id ~ '{_SHA256}' AND request_hash ~ '{_SHA256}'",
            name=conv("ck_strategy_learning_event_hashes"),
        ),
        CheckConstraint(
            f"experiment_id IS NULL OR experiment_id ~ '{_SHA256}'",
            name=conv("ck_strategy_learning_event_experiment_id"),
        ),
        CheckConstraint(
            f"stage IN ({_STAGES_SQL})",
            name=conv("ck_strategy_learning_event_stage"),
        ),
        CheckConstraint(
            f"verdict IN ({_VERDICTS_SQL})",
            name=conv("ck_strategy_learning_event_verdict"),
        ),
        CheckConstraint(
            f"failure_class IN ({_FAILURE_CLASSES_SQL})",
            name=conv("ck_strategy_learning_event_failure_class"),
        ),
        CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array' "
            "AND jsonb_array_length(reason_codes) > 0",
            name=conv("ck_strategy_learning_event_reason_codes"),
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name=conv("ck_strategy_learning_event_evidence_refs"),
        ),
        CheckConstraint(
            "jsonb_typeof(failure_fingerprint) = 'array' "
            "AND jsonb_array_length(failure_fingerprint) = 2",
            name=conv("ck_strategy_learning_event_failure_fingerprint"),
        ),
        CheckConstraint(
            "jsonb_typeof(learning_payload) = 'array' "
            "AND jsonb_array_length(learning_payload) = 2",
            name=conv("ck_strategy_learning_event_learning_payload"),
        ),
        CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(actor_id) <> '' "
            "AND btrim(actor_role) <> ''",
            name=conv("ck_strategy_learning_event_nonblank_audit"),
        ),
        Index(
            "ix_strategy_learning_event_experiment_created",
            "experiment_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_strategy_learning_event_failure_created",
            "failure_class",
            text("created_at DESC"),
            text("id DESC"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memory_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_strategy_learning_event_experiment",
        ),
        nullable=True,
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_class: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    failure_fingerprint: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    learning_payload: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "LEARNING_EVENT_FAILURE_CLASSES",
    "LEARNING_EVENT_STAGES",
    "LEARNING_EVENT_VERDICTS",
    "ResearchStrategyLearningEvent",
]
