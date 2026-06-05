from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketValuationSnapshot(Base):
    __tablename__ = "market_valuation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_market_valuation_snapshots_market_symbol_date_source",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_market_valuation_snapshots_market",
        ),
        CheckConstraint(
            "source IN ('naver_finance', 'yahoo')",
            name="ck_market_valuation_snapshots_source",
        ),
        Index(
            "ix_market_valuation_snapshots_market_date",
            "market",
            "snapshot_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    per: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    high_52w: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    low_52w: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    # ROB-440 PR3: date the 52-week high occurred (US, from yfinance OHLC) —
    # powers undervalued_breakout date-recency parity. NULL for KR.
    high_52w_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
