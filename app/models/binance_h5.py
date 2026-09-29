"""Durable H5 signal, intent and NAV state; independent of DFC identities."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BinanceH5Signal(Base):
    __tablename__ = "binance_h5_signals"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_binance_h5_signal_correlation"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_h5_signal_side"),
        CheckConstraint(
            "state IN ('observed','entry_reserved','holding','closed','blocked','uncertain')",
            name="ck_binance_h5_signal_state",
        ),
        {"schema": "review"},
    )

    signal_key: Mapped[str] = mapped_column(Text, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    decision_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signal_price_text: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="observed")
    entry_client_order_id: Mapped[str | None] = mapped_column(Text)
    entry_nav_usdt: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    entry_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0
    )
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    entered_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    closed_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0
    )
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0
    )
    fees_usdt: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0
    )
    exit_reason: Mapped[str | None] = mapped_column(Text)
    exit_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exit_bar_close_ts: Mapped[int | None] = mapped_column(BigInteger)
    forecast_id: Mapped[str | None] = mapped_column(Text)
    forecast_resolved_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class BinanceH5Intent(Base):
    __tablename__ = "binance_h5_intents"
    __table_args__ = (
        UniqueConstraint("signal_key", "leg_key", name="uq_binance_h5_intent_leg"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_h5_intent_side"),
        CheckConstraint(
            "state IN ('reserved','sending','acknowledged','evidenced','settled','uncertain')",
            name="ck_binance_h5_intent_state",
        ),
        CheckConstraint("qty > 0", name="ck_binance_h5_intent_qty_positive"),
        CheckConstraint(
            "executed_qty >= 0", name="ck_binance_h5_intent_executed_nonnegative"
        ),
        {"schema": "review"},
    )

    client_order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    signal_key: Mapped[str] = mapped_column(
        Text, ForeignKey("review.binance_h5_signals.signal_key"), nullable=False
    )
    leg_key: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="reserved")
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    broker_status: Mapped[str | None] = mapped_column(Text)
    executed_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0
    )
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class BinanceH5LaneState(Base):
    __tablename__ = "binance_h5_lane_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_binance_h5_lane_singleton"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    day_kst: Mapped[dt.date] = mapped_column(Date, nullable=False)
    day_start_nav_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    peak_nav_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    last_nav_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    day_entry_halted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    halt_reason: Mapped[str | None] = mapped_column(Text)
    last_decision_ts: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class BinanceH5Opportunity(Base):
    __tablename__ = "binance_h5_opportunities"
    __table_args__ = ({"schema": "review"},)

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bar_open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    bar_high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    bar_low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    bar_close_text: Mapped[str] = mapped_column(Text, nullable=False)
    bid: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    ask: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class BinanceH5NavSample(Base):
    __tablename__ = "binance_h5_nav_samples"
    __table_args__ = ({"schema": "review"},)

    observed_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), primary_key=True
    )
    nav_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
