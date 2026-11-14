"""Durable TREND-TSMOM-v1 signal and send-intent state (hk 3796 CT-3).

Independent of the H5 and DFC identities. Written only by
``app.services.brokers.binance.trend.state``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SIGNAL_STATES = (
    "observed",
    "entry_reserved",
    "holding",
    "closed",
    "blocked",
    "uncertain",
    "conflict_blocked",
    "conflict_rejected",
)
INTENT_STATES = (
    "reserved",
    "sending",
    "acknowledged",
    "evidenced",
    "settled",
    "uncertain",
    "rejected",
)


def _in_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


class BinanceTrendSignal(Base):
    __tablename__ = "binance_trend_signals"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_binance_trend_signal_correlation"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_trend_signal_side"),
        CheckConstraint(
            f"state IN ({_in_list(SIGNAL_STATES)})",
            name="ck_binance_trend_signal_state",
        ),
        CheckConstraint(
            "entry_notional_usdt IS NULL OR entry_notional_usdt > 0",
            name="ck_binance_trend_signal_notional_positive",
        ),
        CheckConstraint(
            "state NOT IN ('entry_reserved','holding','uncertain') "
            "OR entry_notional_usdt IS NOT NULL",
            name="ck_binance_trend_signal_active_notional",
        ),
        CheckConstraint(
            "(state IN ('conflict_blocked','conflict_rejected')) "
            "= (conflict_at IS NOT NULL)",
            name="ck_binance_trend_signal_conflict_record",
        ),
        CheckConstraint(
            "conflict_detail IS NULL OR char_length(conflict_detail) <= 200",
            name="ck_binance_trend_signal_conflict_detail",
        ),
        Index("ix_binance_trend_signals_symbol_state", "symbol", "state"),
        {"schema": "review"},
    )

    signal_key: Mapped[str] = mapped_column(Text, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    decision_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signal_price_text: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="observed")
    entry_notional_usdt: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    entry_client_order_id: Mapped[str | None] = mapped_column(Text)
    entry_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0, server_default="0"
    )
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    entered_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    closed_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0, server_default="0"
    )
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0, server_default="0"
    )
    fees_usdt: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0, server_default="0"
    )
    exit_reason: Mapped[str | None] = mapped_column(Text)
    exit_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    conflict_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    conflict_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class BinanceTrendIntent(Base):
    __tablename__ = "binance_trend_intents"
    __table_args__ = (
        UniqueConstraint("signal_key", "leg_key", name="uq_binance_trend_intent_leg"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_trend_intent_side"),
        CheckConstraint(
            f"state IN ({_in_list(INTENT_STATES)})",
            name="ck_binance_trend_intent_state",
        ),
        CheckConstraint("qty > 0", name="ck_binance_trend_intent_qty_positive"),
        CheckConstraint(
            "executed_qty >= 0", name="ck_binance_trend_intent_executed_nonnegative"
        ),
        Index("ix_binance_trend_intents_signal", "signal_key"),
        {"schema": "review"},
    )

    client_order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    signal_key: Mapped[str] = mapped_column(
        Text, ForeignKey("review.binance_trend_signals.signal_key"), nullable=False
    )
    leg_key: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="reserved")
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    broker_status: Mapped[str | None] = mapped_column(Text)
    executed_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=0, server_default="0"
    )
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
