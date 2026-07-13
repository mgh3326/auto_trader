"""Immutable ROB-849 cohort, canonical snapshot, and native-link models."""

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv
from sqlalchemy.sql import func

from app.models.base import Base

_SHA256 = "^[0-9a-f]{64}$"


class PaperValidationCohort(Base):
    __tablename__ = "paper_validation_cohorts"
    __table_args__ = (
        UniqueConstraint("cohort_id", name="uq_paper_validation_cohort_id"),
        CheckConstraint(
            'venues = \'["binance", "alpaca"]\'::jsonb',
            name=conv("ck_paper_validation_cohort_venues"),
        ),
        CheckConstraint(
            'symbols = \'["BTCUSDT", "ETHUSDT"]\'::jsonb',
            name=conv("ck_paper_validation_cohort_symbols"),
        ),
        CheckConstraint(
            "market = 'spot'", name=conv("ck_paper_validation_cohort_market")
        ),
        CheckConstraint(
            "leverage = 1", name=conv("ck_paper_validation_cohort_leverage")
        ),
        CheckConstraint(
            "interval = '1m'", name=conv("ck_paper_validation_cohort_interval")
        ),
        CheckConstraint(
            "required_lookback > 0 AND max_capture_skew_ms > 0 "
            "AND max_ticker_age_ms > 0",
            name=conv("ck_paper_validation_cohort_capture_limits"),
        ),
        CheckConstraint(
            "capital_notional_usd > 0",
            name=conv("ck_paper_validation_cohort_capital"),
        ),
        CheckConstraint(
            f"cohort_hash ~ '{_SHA256}'",
            name=conv("ck_paper_validation_cohort_hash"),
        ),
        CheckConstraint(
            "stop_at IS NULL OR stop_at > activated_at",
            name=conv("ck_paper_validation_cohort_times"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venues: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    required_lookback: Mapped[int] = mapped_column(Integer, nullable=False)
    max_capture_skew_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    max_ticker_age_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    capital_notional_usd: Mapped[Decimal] = mapped_column(
        Numeric(24, 12), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    stop_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperValidationCohortAssignment(Base):
    __tablename__ = "paper_validation_cohort_assignments"
    __table_args__ = (
        UniqueConstraint("assignment_id", name="uq_paper_cohort_assignment_id"),
        UniqueConstraint(
            "cohort_id", "ordinal", name="uq_paper_cohort_assignment_ordinal"
        ),
        UniqueConstraint(
            "cohort_id",
            "experiment_id",
            name="uq_paper_cohort_assignment_experiment",
        ),
        UniqueConstraint(
            "cohort_id",
            "validation_id",
            name="uq_paper_cohort_assignment_validation",
        ),
        CheckConstraint(
            "(role = 'champion' AND ordinal = 0) OR "
            "(role = 'challenger' AND ordinal IN (1, 2))",
            name=conv("ck_paper_cohort_assignment_role_ordinal"),
        ),
        CheckConstraint(
            "experiment_hash = experiment_id AND "
            + " AND ".join(
                f"{name} ~ '{_SHA256}'"
                for name in (
                    "experiment_hash",
                    "strategy_hash",
                    "config_hash",
                    "policy_hash",
                    "input_hash",
                )
            ),
            name=conv("ck_paper_cohort_assignment_hashes"),
        ),
        CheckConstraint(
            "jsonb_typeof(target_weights) = 'object' "
            "AND target_weights ?& ARRAY['BTCUSDT','ETHUSDT'] "
            "AND (target_weights - ARRAY['BTCUSDT','ETHUSDT']) = '{}'::jsonb "
            "AND (target_weights->>'BTCUSDT')::numeric > 0 "
            "AND (target_weights->>'ETHUSDT')::numeric > 0 "
            "AND ((target_weights->>'BTCUSDT')::numeric + "
            "(target_weights->>'ETHUSDT')::numeric) <= 1",
            name=conv("ck_paper_cohort_assignment_weights"),
        ),
        Index("ix_paper_cohort_assignment_cohort", "cohort_id", "ordinal"),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.paper_validation_cohorts.cohort_id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_cohort",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_version: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_experiment",
        ),
        nullable=False,
    )
    source_backtest_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "research.backtest_runs.id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_assignment_backtest_run",
        ),
        nullable=False,
    )
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_weights: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    experiment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalMarketSnapshot(Base):
    __tablename__ = "canonical_market_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", name="uq_canonical_snapshot_id"),
        UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            name="uq_canonical_snapshot_round",
        ),
        CheckConstraint(
            "schema_id = 'canonical_market_snapshot.v1'",
            name=conv("ck_canonical_snapshot_schema"),
        ),
        CheckConstraint(
            "source = 'binance_public_spot'",
            name=conv("ck_canonical_snapshot_source"),
        ),
        CheckConstraint(
            "host = 'https://api.binance.com'",
            name=conv("ck_canonical_snapshot_host"),
        ),
        CheckConstraint("interval = '1m'", name=conv("ck_canonical_snapshot_interval")),
        CheckConstraint(
            f"content_hash ~ '{_SHA256}' AND capture_completed_at >= capture_started_at",
            name=conv("ck_canonical_snapshot_hash_and_time"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.paper_validation_cohorts.cohort_id",
            ondelete="RESTRICT",
            name="fk_canonical_snapshot_cohort",
        ),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    host: Mapped[str] = mapped_column(String(128), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    required_lookback: Mapped[int] = mapped_column(Integer, nullable=False)
    max_capture_skew_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    max_ticker_age_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    capture_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    capture_completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperCohortDecision(Base):
    __tablename__ = "paper_cohort_decisions"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_paper_cohort_decision_id"),
        UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            "assignment_id",
            "symbol",
            name="uq_paper_cohort_decision_identity",
        ),
        CheckConstraint(
            "mode IN ('shadow','paper_active')",
            name=conv("ck_paper_cohort_decision_mode"),
        ),
        CheckConstraint(
            "symbol IN ('BTCUSDT','ETHUSDT')",
            name=conv("ck_paper_cohort_decision_symbol"),
        ),
        CheckConstraint(
            f"snapshot_hash ~ '{_SHA256}' AND signal_hash ~ '{_SHA256}'",
            name=conv("ck_paper_cohort_decision_hashes"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.paper_validation_cohort_assignments.assignment_id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_decision_assignment",
        ),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.canonical_market_snapshots.snapshot_id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_decision_snapshot",
        ),
        nullable=False,
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    signal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperCohortVenueIntent(Base):
    __tablename__ = "paper_cohort_venue_intents"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_paper_cohort_venue_intent_id"),
        UniqueConstraint("decision_id", "venue", name="uq_paper_cohort_venue_intent"),
        CheckConstraint(
            "venue IN ('binance','alpaca')",
            name=conv("ck_paper_cohort_venue_intent_venue"),
        ),
        CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=conv("ck_paper_cohort_venue_intent_hash"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "research.paper_cohort_decisions.decision_id",
            ondelete="RESTRICT",
            name="fk_paper_cohort_venue_intent_decision",
        ),
        nullable=False,
    )
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    request_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_quote_evidence: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False
    )
    would_order_evidence: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperCohortRunClaim(Base):
    __tablename__ = "paper_cohort_run_claims"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "run_id",
            "round_decision_id",
            name="uq_paper_cohort_run_claim",
        ),
        CheckConstraint(
            f"request_hash ~ '{_SHA256}'",
            name=conv("ck_paper_cohort_run_claim_hash"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_token: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperRunOrderLink(Base):
    __tablename__ = "paper_run_order_links"
    __table_args__ = (
        UniqueConstraint(
            "cohort_id",
            "run_id",
            "decision_id",
            "venue",
            name="uq_paper_run_order_link_intent",
        ),
        UniqueConstraint(
            "native_ledger_kind",
            "native_ledger_row_id",
            name="uq_paper_run_order_link_native_row",
        ),
        UniqueConstraint(
            "venue",
            "client_order_id",
            name="uq_paper_run_order_link_client_order",
        ),
        CheckConstraint(
            "venue IN ('binance','alpaca')",
            name=conv("ck_paper_run_order_link_venue"),
        ),
        CheckConstraint(
            "native_ledger_kind IN ('binance_demo_order_ledger',"
            "'alpaca_paper_order_ledger')",
            name=conv("ck_paper_run_order_link_ledger_kind"),
        ),
        CheckConstraint(
            f"snapshot_hash ~ '{_SHA256}'",
            name=conv("ck_paper_run_order_link_snapshot_hash"),
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    native_ledger_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    native_ledger_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "CanonicalMarketSnapshot",
    "PaperCohortDecision",
    "PaperCohortRunClaim",
    "PaperCohortVenueIntent",
    "PaperRunOrderLink",
    "PaperValidationCohort",
    "PaperValidationCohortAssignment",
]
