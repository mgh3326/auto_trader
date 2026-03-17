"""Trade review system models (review schema)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.trading import InstrumentType


# ---------------------------------------------------------------------------
# review.trades — executed trade records
# ---------------------------------------------------------------------------
class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("account", "order_id", name="uq_review_trades_account_order"),
        CheckConstraint("side IN ('buy','sell')", name="review_trades_side"),
        CheckConstraint("currency IN ('KRW','USD')", name="review_trades_currency"),
        Index("ix_review_trades_trade_date", "trade_date"),
        Index("ix_review_trades_symbol", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    side: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="KRW")
    account: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.trade_snapshots — indicator snapshot at execution time
# ---------------------------------------------------------------------------
class TradeSnapshot(Base):
    __tablename__ = "trade_snapshots"
    __table_args__ = (
        UniqueConstraint("trade_id", name="uq_review_trade_snapshots_trade_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("review.trades.id", ondelete="CASCADE"), nullable=False
    )
    rsi_14: Mapped[float | None] = mapped_column(Numeric(6, 2))
    rsi_7: Mapped[float | None] = mapped_column(Numeric(6, 2))
    ema_20: Mapped[float | None] = mapped_column(Numeric(20, 4))
    ema_200: Mapped[float | None] = mapped_column(Numeric(20, 4))
    macd: Mapped[float | None] = mapped_column(Numeric(20, 4))
    macd_signal: Mapped[float | None] = mapped_column(Numeric(20, 4))
    adx: Mapped[float | None] = mapped_column(Numeric(6, 2))
    stoch_rsi_k: Mapped[float | None] = mapped_column(Numeric(6, 2))
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(10, 2))
    fear_greed: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.trade_reviews — post-trade evaluation
# ---------------------------------------------------------------------------
class TradeReview(Base):
    __tablename__ = "trade_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('good','neutral','bad')", name="review_trade_reviews_verdict"
        ),
        CheckConstraint(
            "review_type IN ('daily','weekly','monthly','manual')",
            name="review_trade_reviews_review_type",
        ),
        Index("ix_review_trade_reviews_trade_type", "trade_id", "review_type"),
        Index("ix_review_trade_reviews_review_date", "review_date"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("review.trades.id", ondelete="CASCADE"), nullable=False
    )
    review_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    price_at_review: Mapped[float | None] = mapped_column(Numeric(20, 4))
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    review_type: Mapped[str] = mapped_column(Text, nullable=False, default="daily")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.pending_snapshots — unfilled order monitoring
# ---------------------------------------------------------------------------
class PendingSnapshot(Base):
    __tablename__ = "pending_snapshots"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="review_pending_side"),
        CheckConstraint(
            "resolved_as IN ('pending','filled','cancelled','expired')",
            name="review_pending_resolved_as",
        ),
        Index("ix_review_pending_resolved_date", "resolved_as", "snapshot_date"),
        Index(
            "ix_review_pending_account_order_date",
            "account",
            "order_id",
            "snapshot_date",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_price: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(20, 4))
    gap_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    days_pending: Mapped[int | None] = mapped_column(Integer)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(Text)
    resolved_as: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
