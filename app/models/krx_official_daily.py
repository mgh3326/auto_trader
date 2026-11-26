"""Append-only KRX Open API official daily trading facts (hk 6586)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import TIMESTAMP, Boolean, CheckConstraint, Date, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KRXOfficialDaily(Base):
    __tablename__ = "krx_official_daily"
    __table_args__ = (
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="symbol_short_code"),
        CheckConstraint("market IN ('kospi','kosdaq')", name="market"),
        CheckConstraint("volume IS NULL OR volume >= 0", name="volume_nonnegative"),
        CheckConstraint(
            "trade_value IS NULL OR trade_value >= 0",
            name="trade_value_nonnegative",
        ),
        CheckConstraint(
            "(volume IS NULL AND full_day_zero_volume IS NULL) "
            "OR (volume IS NOT NULL AND full_day_zero_volume = (volume = 0))",
            name="zero_volume_matches_volume",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(6), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    open_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    low_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    trade_value: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    listed_shares: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    full_day_zero_volume: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    field_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
