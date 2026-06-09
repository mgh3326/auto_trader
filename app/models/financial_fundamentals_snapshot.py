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
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FinancialFundamentalsSnapshot(Base):
    """KR multi-period financial-statement + dividend snapshot (fiscal-period grain).

    One row per (market, symbol, fiscal_period, source). Stores raw per-period facts
    plus cumulative-differenced single-quarter values. Aggregate metrics (3y-avg /
    streaks / TTM / QoQ) are DERIVED in the read-path (derive.py) from the rows visible
    as of report_date — never stored — to avoid lookahead leakage (ROB-330).
    """

    __tablename__ = "financial_fundamentals_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "fiscal_period",
            "source",
            name="uq_financial_fundamentals_snapshots_msfs",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_financial_fundamentals_snapshots_market",
        ),
        CheckConstraint(
            "period_type IN ('annual', 'quarterly')",
            name="ck_financial_fundamentals_snapshots_period_type",
        ),
        CheckConstraint(
            # ROB-441: KR=DART; US non-DART vendors (yfinance statements, finnhub).
            "source IN ('dart', 'yfinance', 'finnhub')",
            name="ck_financial_fundamentals_snapshots_source",
        ),
        CheckConstraint(
            "data_state IN ('fresh', 'stale', 'partial', 'unavailable')",
            name="ck_financial_fundamentals_snapshots_data_state",
        ),
        Index(
            "ix_financial_fundamentals_snapshots_market_symbol_period_end",
            "market",
            "symbol",
            "period_end_date",
        ),
        Index(
            "ix_financial_fundamentals_snapshots_market_symbol_filing",
            "market",
            "symbol",
            "filing_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(10), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    filing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    cost_of_sales: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    payout_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    dividend_per_share: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 4), nullable=True
    )
    discrete_revenue: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 2), nullable=True
    )
    discrete_net_income: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 2), nullable=True
    )
    data_state: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="fresh"
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
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
