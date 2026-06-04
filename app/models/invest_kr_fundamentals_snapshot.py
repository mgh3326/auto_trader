from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InvestKrFundamentalsSnapshot(Base):
    """Daily tvscreener-backed KR valuation + fundamentals snapshot (ROB-428).

    Mirrors ``InvestCryptoScreenerSnapshot``: a daily snapshot keyed by
    ``(symbol, snapshot_date)`` so the KR ``/invest/screener`` can read
    meaningful filled fundamentals/valuation rows (PR-B, out of scope here).
    All numeric columns are nullable because tvscreener coverage is sparse.
    """

    __tablename__ = "invest_kr_fundamentals_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "snapshot_date",
            name="uq_invest_kr_fundamentals_snapshots_symbol_date",
        ),
        CheckConstraint(
            "source IN ('tvscreener_kr')",
            name="source",
        ),
        Index("ix_invest_kr_fundamentals_snapshots_date", "snapshot_date"),
        Index(
            "ix_invest_kr_fundamentals_snapshots_date_roe",
            "snapshot_date",
            "roe_ttm",
        ),
        Index(
            "ix_invest_kr_fundamentals_snapshots_date_per",
            "snapshot_date",
            "per",
        ),
        Index(
            "ix_invest_kr_fundamentals_snapshots_date_dividend_yield",
            "snapshot_date",
            "dividend_yield",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Quote / market columns
    price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(28, 4), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(28, 4), nullable=True)

    # Valuation columns
    per: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )

    # Fundamentals columns
    roe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    payout_ratio_ttm: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    gross_margin_ttm: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    revenue_yoy: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    eps_yoy: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    eps_qoq: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    net_income_yoy: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    net_income_cagr_5y: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )

    # Dividend streak columns (integer-valued counts persisted as Numeric)
    continuous_dividend_payout: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    continuous_dividend_growth: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    # Technical / categorisation columns
    week_high_52: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    # ROB-430 PR-②: the DATE the 52-week high was set (tvscreener
    # PRICE_52_WEEK_HIGH_DATE). undervalued_breakout's Toss "신고가" = a NEW 52w-high
    # made within ~20 days (a breakout event), NOT proximity to the high. nullable
    # because coverage is sparse / the column was added after initial rows.
    week_high_52_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    rsi14: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(120), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)

    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
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
