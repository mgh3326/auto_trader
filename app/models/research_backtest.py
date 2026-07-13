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
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base

# ROB-846 — every attempted trial (invocation) is recorded under one of these
# terminal outcomes. No winner-only filtering: crashed/timeout/rejected trials
# each occupy a monotonic trial_index just like completed ones.
TRIAL_STATUSES: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")


class ResearchStrategyExperiment(Base):
    """ROB-846 — immutable strategy experiment parent.

    Registers a strategy *version* by its canonical SHA-256 identity
    (strategy/code/params/dataset/PIT/frozen-config/policy/benchmark/cost/MDD).
    Rows are append-only and immutable: corrections create a new version linked
    by ``supersedes_experiment_id`` (a new lineage) rather than mutating hashes.
    DB triggers reject UPDATE/DELETE (see migration + test schema mirror).
    """

    __tablename__ = "strategy_experiments"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", name="uq_research_strategy_experiments_experiment_id"
        ),
        Index(
            "ix_research_strategy_experiments_strategy_key",
            "strategy_key",
            "strategy_version",
        ),
        Index(
            "ix_research_strategy_experiments_supersedes",
            "supersedes_experiment_id",
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Canonical identity digest (derive_experiment_id). Immutable, unique.
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)

    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pit_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    benchmark_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mdd_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Raw definitions kept for audit/reproduction (the source of the hashes).
    benchmark_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    mdd_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Lineage: a correction supersedes an earlier immutable version.
    supersedes_experiment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "research.strategy_experiments.experiment_id",
            ondelete="RESTRICT",
            name="fk_research_strategy_experiments_supersedes",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    trials: Mapped[list[ResearchBacktestRun]] = relationship(
        back_populates="experiment",
    )


class ResearchBacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_backtest_runs_run_id"),
        Index("ix_research_backtest_runs_runner", "runner"),
        Index("ix_research_backtest_runs_strategy", "strategy_name"),
        # ROB-846 trial accounting: one monotonic index per experiment, and
        # idempotency dedup per experiment. NULLs (legacy summary rows) are
        # distinct in Postgres, so pre-ROB-846 rows never collide here.
        UniqueConstraint(
            "strategy_experiment_id",
            "trial_index",
            name="uq_research_backtest_runs_experiment_trial_index",
        ),
        UniqueConstraint(
            "strategy_experiment_id",
            "trial_idempotency_key",
            name="uq_research_backtest_runs_experiment_idempotency",
        ),
        CheckConstraint(
            "trial_status IS NULL OR trial_status IN "
            "('completed','rejected','crashed','timeout')",
            name="ck_research_backtest_runs_trial_status",
        ),
        Index(
            "ix_research_backtest_runs_experiment",
            "strategy_experiment_id",
            "trial_index",
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="spot")
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    timerange: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runner: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_trades: Mapped[int] = mapped_column(nullable=False, default=0)
    profit_factor: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    max_drawdown: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    expectancy: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # ROB-846 append-only trial-child fields (nullable → legacy summary rows
    # ingested via upsert_backtest_run leave these NULL and stay mutable; rows
    # with a non-null strategy_experiment_id are immutable trials).
    strategy_experiment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "research.strategy_experiments.id",
            ondelete="RESTRICT",
            name="fk_research_backtest_runs_experiment_id",
        ),
        nullable=True,
    )
    trial_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    information_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gate_artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trial_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    experiment: Mapped[ResearchStrategyExperiment | None] = relationship(
        back_populates="trials",
    )

    pairs: Mapped[list[ResearchBacktestPair]] = relationship(
        back_populates="backtest_run",
        cascade="all, delete-orphan",
    )
    candidate: Mapped[ResearchPromotionCandidate | None] = relationship(
        back_populates="backtest_run",
        uselist=False,
        cascade="all, delete-orphan",
    )
    sync_jobs: Mapped[list[ResearchSyncJob]] = relationship(
        back_populates="backtest_run",
    )


class ResearchBacktestPair(Base):
    __tablename__ = "backtest_pairs"
    __table_args__ = (
        UniqueConstraint(
            "backtest_run_id",
            "pair",
            name="uq_research_backtest_pairs_run_pair",
        ),
        Index("ix_research_backtest_pairs_run_id", "backtest_run_id"),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backtest_run_id: Mapped[int] = mapped_column(
        ForeignKey("research.backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    pair: Mapped[str] = mapped_column(String(32), nullable=False)
    total_trades: Mapped[int] = mapped_column(nullable=False, default=0)
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    backtest_run: Mapped[ResearchBacktestRun] = relationship(back_populates="pairs")


class ResearchPromotionCandidate(Base):
    __tablename__ = "promotion_candidates"
    __table_args__ = (
        UniqueConstraint(
            "backtest_run_id",
            name="uq_research_promotion_candidates_run_id",
        ),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backtest_run_id: Mapped[int] = mapped_column(
        ForeignKey("research.backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    thresholds: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # ROB-846 — a promotion candidate is a deterministic evaluation output
    # linked to an EXACT experiment/config/data identity. The registry refuses
    # to link a candidate whose hashes are missing or mismatch the experiment.
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_data_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    backtest_run: Mapped[ResearchBacktestRun] = relationship(back_populates="candidate")


class ResearchSyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_research_sync_jobs_idempotency"),
        Index("ix_research_sync_jobs_status", "status"),
        {"schema": "research"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    backtest_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("research.backtest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    backtest_run: Mapped[ResearchBacktestRun | None] = relationship(
        back_populates="sync_jobs"
    )
