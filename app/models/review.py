"""Trade review system models (review schema)."""

from __future__ import annotations

import uuid
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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


# ---------------------------------------------------------------------------
# review.kis_mock_order_ledger — KIS official-mock execution records (ROB-37)
# Fully isolated from live review.trades / TradeJournal paths.
# ---------------------------------------------------------------------------
class KISMockOrderLedger(Base):
    __tablename__ = "kis_mock_order_ledger"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_kis_mock_ledger_order_no"),
        CheckConstraint("side IN ('buy','sell')", name="kis_mock_ledger_side"),
        CheckConstraint("currency IN ('KRW','USD')", name="kis_mock_ledger_currency"),
        CheckConstraint(
            "account_mode = 'kis_mock'", name="kis_mock_ledger_account_mode_kis_mock"
        ),
        CheckConstraint("broker = 'kis'", name="kis_mock_ledger_broker_kis"),
        CheckConstraint(
            "status IN ('accepted','rejected','unknown')",
            name="kis_mock_ledger_status_allowed",
        ),
        Index("ix_kis_mock_ledger_trade_date", "trade_date"),
        Index("ix_kis_mock_ledger_symbol", "symbol"),
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
    order_type: Mapped[str] = mapped_column(Text, nullable=False, default="limit")
    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    fee: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="KRW")

    order_no: Mapped[str | None] = mapped_column(Text)
    order_time: Mapped[str | None] = mapped_column(Text)
    krx_fwdg_ord_orgno: Mapped[str | None] = mapped_column(Text)

    account_mode: Mapped[str] = mapped_column(Text, nullable=False, default="kis_mock")
    broker: Mapped[str] = mapped_column(Text, nullable=False, default="kis")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# review.alpaca_paper_order_ledger — Alpaca Paper execution lifecycle ledger (ROB-84)
# Records preview → validation → submit → status/fill → cancel → position → reconcile.
# Fully isolated from live trade paths and review.trades.
# All writes must go through AlpacaPaperLedgerService. No direct SQL writes.
# ---------------------------------------------------------------------------
class AlpacaPaperOrderLedger(Base):
    __tablename__ = "alpaca_paper_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id", name="uq_alpaca_paper_ledger_client_order_id"
        ),
        CheckConstraint("broker = 'alpaca'", name="alpaca_paper_ledger_broker"),
        CheckConstraint(
            "account_mode = 'alpaca_paper'", name="alpaca_paper_ledger_account_mode"
        ),
        CheckConstraint(
            "lifecycle_state IN ("
            "'previewed','validation_failed','submitted','open',"
            "'partially_filled','filled','canceled','unexpected'"
            ")",
            name="alpaca_paper_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('buy','sell')", name="alpaca_paper_ledger_side"),
        CheckConstraint(
            "order_type IN ('limit','market')", name="alpaca_paper_ledger_order_type"
        ),
        CheckConstraint(
            "currency IN ('USD','KRW')", name="alpaca_paper_ledger_currency"
        ),
        Index("ix_alpaca_paper_ledger_broker_order_id", "broker_order_id"),
        Index("ix_alpaca_paper_ledger_lifecycle_state", "lifecycle_state"),
        Index("ix_alpaca_paper_ledger_created_at", "created_at"),
        Index("ix_alpaca_paper_ledger_candidate_uuid", "candidate_uuid"),
        Index(
            "ix_alpaca_paper_ledger_briefing_run_uuid",
            "briefing_artifact_run_uuid",
        ),
        Index("ix_alpaca_paper_ledger_execution_symbol", "execution_symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Correlation key
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Broker/mode identity — pinned, never live
    broker: Mapped[str] = mapped_column(Text, nullable=False, default="alpaca")
    account_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="alpaca_paper"
    )

    # Application lifecycle state
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

    # Signal provenance (kept separate from execution)
    signal_symbol: Mapped[str | None] = mapped_column(Text)
    signal_venue: Mapped[str | None] = mapped_column(Text)

    # Execution venue/symbol
    execution_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    execution_venue: Mapped[str] = mapped_column(Text, nullable=False)
    execution_asset_class: Mapped[str | None] = mapped_column(Text)

    # Instrument
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )

    # Order parameters
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False, default="limit")
    time_in_force: Mapped[str | None] = mapped_column(Text)
    requested_qty: Mapped[float | None] = mapped_column(Numeric(20, 8))
    requested_notional: Mapped[float | None] = mapped_column(Numeric(20, 4))
    requested_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="USD")

    # Sanitized event payloads
    preview_payload: Mapped[dict | None] = mapped_column(JSONB)
    validation_summary: Mapped[dict | None] = mapped_column(JSONB)

    # Broker order fields
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    order_status: Mapped[str | None] = mapped_column(Text)
    filled_qty: Mapped[float | None] = mapped_column(Numeric(20, 8))
    filled_avg_price: Mapped[float | None] = mapped_column(Numeric(20, 8))

    # Cancel tracking
    cancel_status: Mapped[str | None] = mapped_column(Text)
    canceled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Position snapshot — null=not checked; {qty,avg_entry_price,fetched_at}=checked
    position_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # Reconcile
    reconcile_status: Mapped[str | None] = mapped_column(Text)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Approval provenance (no FK; artifact is transient)
    briefing_artifact_run_uuid: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    briefing_artifact_status: Mapped[str | None] = mapped_column(Text)
    qa_evaluator_status: Mapped[str | None] = mapped_column(Text)
    approval_bridge_generated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_bridge_status: Mapped[str | None] = mapped_column(Text)
    candidate_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # Workflow context
    workflow_stage: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)

    # Accumulated sanitized event map keyed by event type
    raw_responses: Mapped[dict | None] = mapped_column(JSONB)

    # Operator notes
    notes: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
