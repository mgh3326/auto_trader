"""Immutable ROB-850 paper-evaluation models.

All four tables live in the ``research`` schema and are append-only:
``BEFORE UPDATE OR DELETE`` and ``BEFORE TRUNCATE`` triggers (created by the
ROB-850 Alembic migration and mirrored in the test schema bootstrap) reject
every mutation.  No ``updated_at`` column exists on any of these models.

* ``EvaluationConfig``      — content-addressed evaluation configuration
* ``EvaluationEpoch``       — per-cohort evaluation epoch (reset boundary)
* ``EvaluationScorecard``   — one row per epoch per view (three views)
* ``EvaluationVerdict``     — one row per epoch (conjunctive verdict)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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

_SHA256 = "^[0-9a-f]{64}$"


class EvaluationConfig(Base):
    """Content-addressed evaluation configuration (immutable)."""

    __tablename__ = "evaluation_configs"
    __table_args__ = (
        UniqueConstraint("config_hash", name="uq_evaluation_config_hash"),
        CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_config_hash"),
        ),
        CheckConstraint(
            "schema_id = 'paper_evaluation_config.v1'",
            name=conv("ck_evaluation_config_schema_id"),
        ),
        CheckConstraint(
            "formula_version = 'v1'",
            name=conv("ck_evaluation_config_formula_version"),
        ),
        CheckConstraint(
            "currency_conversion_policy = 'none'",
            name=conv("ck_evaluation_config_currency_conversion_policy"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_id: Mapped[str] = mapped_column(String(64), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(16), nullable=False)
    currency_conversion_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationEpoch(Base):
    """Per-cohort evaluation epoch — reset boundary (immutable)."""

    __tablename__ = "evaluation_epochs"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "assignment_id",
            "epoch_id",
            name="uq_evaluation_epoch_lineage",
        ),
        UniqueConstraint(
            "epoch_id",
            "assignment_id",
            "config_hash",
            "experiment_hash",
            "cohort_hash",
            name="uq_evaluation_epoch_identity",
        ),
        UniqueConstraint(
            "cohort_id",
            "assignment_id",
            "config_hash",
            "started_at",
            name="uq_evaluation_epoch_start",
        ),
        ForeignKeyConstraint(
            ["cohort_id", "assignment_id"],
            [
                "research.paper_validation_cohort_assignments.cohort_id",
                "research.paper_validation_cohort_assignments.assignment_id",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_assignment",
        ),
        ForeignKeyConstraint(
            ["cohort_id", "cohort_hash"],
            [
                "research.paper_validation_cohorts.cohort_id",
                "research.paper_validation_cohorts.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_cohort_lineage",
        ),
        ForeignKeyConstraint(
            ["cohort_id", "assignment_id", "prior_epoch_id"],
            [
                "research.evaluation_epochs.cohort_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.epoch_id",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_prior_lineage",
        ),
        CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_epoch_config_hash"),
        ),
        CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_epoch_experiment_hash"),
        ),
        CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_epoch_cohort_hash"),
        ),
        CheckConstraint(
            "((prior_epoch_id IS NULL AND reset_reason IS NULL) OR "
            "(prior_epoch_id IS NOT NULL AND reset_reason IN "
            "('account_reset','api_key_recreation','initial_equity_change')))",
            name=conv("ck_evaluation_epoch_reset_reason"),
        ),
        CheckConstraint(
            "prior_epoch_id IS NULL OR prior_epoch_id <> epoch_id",
            name=conv("ck_evaluation_epoch_prior_not_self"),
        ),
        Index(
            "ix_evaluation_epoch_cohort_started",
            "cohort_id",
            text("started_at DESC"),
        ),
        Index(
            "ix_evaluation_epoch_assignment_started",
            "cohort_id",
            "assignment_id",
            "started_at",
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    epoch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.paper_validation_cohorts.cohort_id",
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_cohort",
        ),
        nullable=False,
    )
    config_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "research.evaluation_configs.config_hash",
            ondelete="RESTRICT",
            name="fk_evaluation_epoch_config",
        ),
        nullable=False,
    )
    initial_equity: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reset_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prior_epoch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationScorecard(Base):
    """Per-epoch per-view scorecard — one row per view (immutable)."""

    __tablename__ = "evaluation_scorecards"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "view_name",
            name="uq_evaluation_scorecard_evaluation_view",
        ),
        ForeignKeyConstraint(
            [
                "epoch_id",
                "assignment_id",
                "config_hash",
                "experiment_hash",
                "cohort_hash",
            ],
            [
                "research.evaluation_epochs.epoch_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.config_hash",
                "research.evaluation_epochs.experiment_hash",
                "research.evaluation_epochs.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_scorecard_epoch_identity",
        ),
        CheckConstraint(
            f"evaluation_id ~ '{_SHA256}'",
            name=conv("ck_evaluation_scorecard_evaluation_id"),
        ),
        CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_scorecard_config_hash"),
        ),
        CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_scorecard_experiment_hash"),
        ),
        CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_scorecard_cohort_hash"),
        ),
        CheckConstraint(
            "view_name IN ('binance_broker','alpaca_broker','canonical_shadow')",
            name=conv("ck_evaluation_scorecard_view_name"),
        ),
        CheckConstraint(
            "currency IN ('USDT','USD')",
            name=conv("ck_evaluation_scorecard_currency"),
        ),
        CheckConstraint(
            "(view_name = 'binance_broker' AND currency = 'USDT') OR "
            "(view_name = 'alpaca_broker' AND currency = 'USD') OR "
            "(view_name = 'canonical_shadow' AND currency = 'USDT')",
            name=conv("ck_evaluation_scorecard_view_currency_consistency"),
        ),
        Index("ix_evaluation_scorecard_epoch", "epoch_id"),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    view_name: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationVerdict(Base):
    """Per-epoch conjunctive verdict — one row per epoch (immutable)."""

    __tablename__ = "evaluation_verdicts"
    __table_args__ = (
        UniqueConstraint("evaluation_id", name="uq_evaluation_verdict_evaluation"),
        UniqueConstraint(
            "epoch_id",
            "idempotency_key",
            name="uq_evaluation_verdict_idempotency",
        ),
        ForeignKeyConstraint(
            [
                "epoch_id",
                "assignment_id",
                "config_hash",
                "experiment_hash",
                "cohort_hash",
            ],
            [
                "research.evaluation_epochs.epoch_id",
                "research.evaluation_epochs.assignment_id",
                "research.evaluation_epochs.config_hash",
                "research.evaluation_epochs.experiment_hash",
                "research.evaluation_epochs.cohort_hash",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_verdict_epoch_identity",
        ),
        CheckConstraint(
            f"evaluation_id ~ '{_SHA256}'",
            name=conv("ck_evaluation_verdict_evaluation_id"),
        ),
        CheckConstraint(
            f"config_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_verdict_config_hash"),
        ),
        CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_verdict_request_hash"),
        ),
        CheckConstraint(
            f"experiment_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_verdict_experiment_hash"),
        ),
        CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=conv("ck_evaluation_verdict_cohort_hash"),
        ),
        CheckConstraint(
            "verdict_status IN ('promotion_eligible','insufficient_evidence',"
            "'gate_blocked','benchmark_not_beaten','mdd_exceeded')",
            name=conv("ck_evaluation_verdict_status"),
        ),
        Index("ix_evaluation_verdict_epoch", "epoch_id"),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict_status: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "EvaluationConfig",
    "EvaluationEpoch",
    "EvaluationScorecard",
    "EvaluationVerdict",
]
