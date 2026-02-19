from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchBacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_backtest_runs_run_id"),
        Index("ix_research_backtest_runs_runner", "runner"),
        Index("ix_research_backtest_runs_strategy", "strategy_name"),
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
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
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
