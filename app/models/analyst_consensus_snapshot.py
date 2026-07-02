from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnalystConsensusSnapshot(Base):
    __tablename__ = "analyst_consensus_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_analyst_consensus_snapshots_market_symbol_date_source",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')", name="ck_analyst_consensus_snapshots_market"
        ),
        CheckConstraint(
            "source IN ('naver_finance', 'yfinance')",
            name="ck_analyst_consensus_snapshots_source",
        ),
        Index(
            "ix_analyst_consensus_snapshots_market_symbol_date",
            "market",
            "symbol",
            "snapshot_date",
        ),
        Index(
            "ix_analyst_consensus_snapshots_market_date",
            "market",
            "snapshot_date",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    buy_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hold_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sell_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strong_buy_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_mean: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    target_median: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    target_high: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    target_low: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    upside_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    analyst_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    newest_opinion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
