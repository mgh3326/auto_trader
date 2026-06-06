from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
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


class InvestCryptoScreenerSnapshot(Base):
    __tablename__ = "invest_crypto_screener_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "snapshot_date",
            name="uq_invest_crypto_screener_snapshots_symbol_date",
        ),
        CheckConstraint(
            "symbol LIKE 'KRW-%'",
            name="symbol",
        ),
        CheckConstraint(
            "source IN ('tvscreener_upbit')",
            name="source",
        ),
        Index("ix_invest_crypto_screener_snapshots_date", "snapshot_date"),
        Index(
            "ix_invest_crypto_screener_snapshots_date_trade_amount",
            "snapshot_date",
            "trade_amount_24h",
        ),
        Index(
            "ix_invest_crypto_screener_snapshots_date_rsi",
            "snapshot_date",
            "rsi",
        ),
        Index(
            "ix_invest_crypto_screener_snapshots_date_change_rate",
            "snapshot_date",
            "change_rate",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latest_close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    trade_amount_24h: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 4), nullable=True
    )
    volume_24h: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    volume_24h_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 4), nullable=True
    )
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(28, 4), nullable=True)
    rsi: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    adx: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    # ROB-443 Phase 1: crypto-native USD-M perp funding rate (lastFundingRate, a
    # ratio e.g. 0.0001; vendor call localized to derivatives.py). NULL for
    # Upbit-only coins without a perp (fail-closed). Powers funding presets.
    funding_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    market_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
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
