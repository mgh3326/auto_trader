"""Trade review system models (review schema)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
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
from sqlalchemy.sql import func, text

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
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','submitted','accepted','pending','fill',"
            "'reconciled','stale','failed','anomaly','cancelled'"
            ")",
            name="kis_mock_ledger_lifecycle_state_allowed",
        ),
        Index("ix_kis_mock_ledger_trade_date", "trade_date"),
        Index("ix_kis_mock_ledger_symbol", "symbol"),
        Index("ix_kis_mock_ledger_lifecycle_state", "lifecycle_state"),
        Index("ix_kis_mock_ledger_correlation_id", "correlation_id"),
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

    # ROB-102 lifecycle columns
    lifecycle_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="anomaly"
    )
    holdings_baseline_qty: Mapped[float | None] = mapped_column(Numeric(20, 8))
    reconcile_attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_reconcile_detail: Mapped[dict | None] = mapped_column(JSONB)

    # ROB-321 round-trip scalping columns (additive, nullable). A buy/sell
    # round trip shares one correlation_id; the exit leg carries exit_reason +
    # gross/net PnL once paired from execution evidence.
    correlation_id: Mapped[str | None] = mapped_column(Text)
    scalping_role: Mapped[str | None] = mapped_column(Text)  # 'entry' | 'exit'
    exit_reason: Mapped[str | None] = mapped_column(
        Text
    )  # stop_loss|take_profit|time_stop
    gross_pnl: Mapped[float | None] = mapped_column(Numeric(20, 4))
    net_pnl: Mapped[float | None] = mapped_column(Numeric(20, 4))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class KISLiveOrderLedger(Base):
    """ROB-395 — KIS live (real-money) order lifecycle ledger.

    Records every accepted/rejected live KR order at SEND time, carrying the
    buy/sell intent. Fills/journals/realized_pnl are NOT booked here; they are
    applied only by kis_live_reconcile_orders from order-id-keyed broker fill
    evidence. Keyed by order_no so multi-order-same-symbol cannot double-book
    (unlike holdings-delta attribution — see ROB-400).
    """

    __tablename__ = "kis_live_order_ledger"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_kis_live_ledger_order_no"),
        Index("ix_kis_live_ledger_status", "status"),
        Index("ix_kis_live_ledger_symbol", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency: Mapped[str | None] = mapped_column(Text)

    order_no: Mapped[str | None] = mapped_column(Text)
    order_time: Mapped[str | None] = mapped_column(Text)
    krx_fwdg_ord_orgno: Mapped[str | None] = mapped_column(Text)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False, default="kis_live")
    broker: Mapped[str] = mapped_column(Text, nullable=False, default="kis")

    # send-time status: accepted | rejected ; reconcile updates to
    # filled | partial | pending | cancelled | anomaly
    status: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    # buy/sell intent captured at send, consumed by reconcile
    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # reconcile outcomes
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    trade_id: Mapped[int | None] = mapped_column(BigInteger)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class LiveOrderLedger(Base):
    """ROB-407 — 제네릭 live (real-money) order lifecycle ledger.

    US/해외(`equity_us`)·crypto(`crypto`) live 주문을 전송 시 accepted-only로 기록한다.
    KISLiveOrderLedger(KR domestic 전용)와 동일 evidence-gated 계약을 따르되,
    broker/market 디스크리미네이터와 시장별 메타(exchange/market_symbol)를 갖는다.
    fill/journal/realized_pnl은 live_reconcile_orders가 broker 체결 증거로만 반영한다.
    """

    __tablename__ = "live_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "broker", "account_scope", "order_no", name="uq_live_ledger_order"
        ),
        Index("ix_live_ledger_status", "status"),
        Index("ix_live_ledger_market_symbol", "market", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    # discriminators / market metadata
    broker: Mapped[str] = mapped_column(Text, nullable=False)  # kis | upbit
    account_scope: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # kis_live | upbit_live
    market: Mapped[str] = mapped_column(Text, nullable=False)  # us | crypto
    symbol: Mapped[str] = mapped_column(Text, nullable=False)  # DB dot-format
    exchange: Mapped[str | None] = mapped_column(Text)  # US: NASD/NYSE/AMEX
    market_symbol: Mapped[str | None] = mapped_column(Text)  # crypto: KRW-BTC

    side: Mapped[str] = mapped_column(Text, nullable=False)  # buy | sell
    order_kind: Mapped[str] = mapped_column(Text, nullable=False)  # market | limit
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(Text)

    order_no: Mapped[str | None] = mapped_column(Text)  # KIS odno / Upbit uuid
    order_time: Mapped[str | None] = mapped_column(Text)

    # send-time status: accepted | rejected ; reconcile updates to
    # filled | partial | pending | cancelled | anomaly
    status: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    # buy/sell intent captured at send, consumed by reconcile
    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # ROB-164 defensive-trim approval audit, captured at send so the
    # evidence-gated journal close (reconcile) can still append the
    # defensive-trim note to the closed journal.
    dt_approval_issue_id: Mapped[str | None] = mapped_column(Text)
    dt_requester_agent_id: Mapped[str | None] = mapped_column(Text)
    dt_caller_source: Mapped[str | None] = mapped_column(Text)

    # reconcile outcomes (filled_qty = 이미 booked된 누적 체결량, 델타 멱등용)
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    trade_id: Mapped[int | None] = mapped_column(BigInteger)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# review.alpaca_paper_order_ledger — Alpaca Paper execution lifecycle ledger (ROB-84/ROB-90)
# Records plan → preview → validation → submit → fill → position → close → final reconcile.
# Fully isolated from live trade paths and review.trades.
# All writes must go through AlpacaPaperLedgerService. No direct SQL writes.
# ---------------------------------------------------------------------------
class AlpacaPaperOrderLedger(Base):
    __tablename__ = "alpaca_paper_order_ledger"
    __table_args__ = (
        # ROB-90: partial unique indexes replace the old single-column unique constraint.
        # Non-validation records are unique by (client_order_id, record_kind).
        Index(
            "uq_alpaca_paper_ledger_client_order_kind",
            "client_order_id",
            "record_kind",
            unique=True,
            postgresql_where=text("validation_attempt_no IS NULL"),
        ),
        # Validation attempts are unique by (correlation_id, side, attempt_no).
        Index(
            "uq_alpaca_paper_ledger_validation_attempt",
            "lifecycle_correlation_id",
            "side",
            "validation_attempt_no",
            unique=True,
            postgresql_where=text("record_kind = 'validation_attempt'"),
        ),
        CheckConstraint("broker = 'alpaca'", name="alpaca_paper_ledger_broker"),
        CheckConstraint(
            "account_mode = 'alpaca_paper'", name="alpaca_paper_ledger_account_mode"
        ),
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'position_reconciled','sell_validated','closed','final_reconciled','anomaly',"
            "'stale_preview_cleanup_required'"
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
        CheckConstraint(
            "record_kind IN ('plan','preview','validation_attempt','execution','reconcile','anomaly')",
            name="alpaca_paper_ledger_record_kind",
        ),
        CheckConstraint(
            "validation_outcome IN ('passed','failed','skipped','n_a')",
            name="alpaca_paper_ledger_validation_outcome",
        ),
        CheckConstraint(
            "leg_role IS NULL OR leg_role IN ('buy','sell','roundtrip')",
            name="alpaca_paper_ledger_leg_role",
        ),
        CheckConstraint(
            "settlement_status IN ('pending','settled','failed','n_a')",
            name="alpaca_paper_ledger_settlement_status",
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
        Index("ix_alpaca_paper_ledger_correlation_id", "lifecycle_correlation_id"),
        Index("ix_alpaca_paper_ledger_record_kind", "record_kind"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Correlation key (per order leg)
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)

    # ROB-90: cross-leg correlation — buy and sell legs share this value.
    # Defaults to client_order_id for backwards compatibility.
    lifecycle_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)

    # ROB-90: record type — distinguishes plan/preview/validation/execution/reconcile rows.
    record_kind: Mapped[str] = mapped_column(Text, nullable=False, default="execution")

    # Broker/mode identity — pinned, never live
    broker: Mapped[str] = mapped_column(Text, nullable=False, default="alpaca")
    account_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="alpaca_paper"
    )

    # Application lifecycle state (ROB-90 canonical)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

    # ROB-90: buy/sell leg role — separate from broker `side` (order direction).
    leg_role: Mapped[str | None] = mapped_column(Text)

    # ROB-90: validation attempt tracking
    validation_attempt_no: Mapped[int | None] = mapped_column(SmallInteger)
    validation_outcome: Mapped[str | None] = mapped_column(Text)

    # ROB-90: confirm flag — false=validation-only, true=executed, null=plan/preview
    confirm_flag: Mapped[bool | None] = mapped_column(Boolean)

    # ROB-90: fee/settlement
    fee_amount: Mapped[float | None] = mapped_column(Numeric(20, 4))
    fee_currency: Mapped[str | None] = mapped_column(Text)
    settlement_status: Mapped[str | None] = mapped_column(Text)
    settlement_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # ROB-90: signed quantity effect (buy positive, sell negative, preview/validation null)
    qty_delta: Mapped[float | None] = mapped_column(Numeric(20, 8))

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


class WatchOrderIntentLedger(Base):
    """ROB-402 — watch-sourced order intent (kis_mock only). Audit of the
    auto-execute decision; the actual order lives in KISMockOrderLedger linked
    by correlation_id. Mirrors migration daf4130b13ce."""

    __tablename__ = "watch_order_intent_ledger"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_watch_intent_correlation_id"),
        CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('buy','sell')", name="watch_intent_ledger_side"),
        CheckConstraint(
            "account_mode = 'kis_mock'", name="watch_intent_ledger_account_mode"
        ),
        CheckConstraint(
            "execution_source = 'watch'", name="watch_intent_ledger_execution_source"
        ),
        CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        Index("ix_watch_intent_kst_date", "kst_date"),
        Index("ix_watch_intent_market_symbol", "market", "symbol"),
        Index("ix_watch_intent_state_created_at", "lifecycle_state", "created_at"),
        Index(
            "uq_watch_intent_previewed_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("lifecycle_state = 'previewed'"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    condition_type: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    threshold_key: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    execution_source: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float | None] = mapped_column(Numeric(18, 8))
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 8))
    notional: Mapped[float | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str | None] = mapped_column(Text)
    notional_krw_input: Mapped[float | None] = mapped_column(Numeric(18, 2))
    max_notional_krw: Mapped[float | None] = mapped_column(Numeric(18, 2))
    notional_krw_evaluated: Mapped[float | None] = mapped_column(Numeric(18, 2))
    fx_usd_krw_used: Mapped[float | None] = mapped_column(Numeric(18, 4))
    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    execution_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    blocking_reasons: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    blocked_by: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    preview_line: Mapped[dict] = mapped_column(JSONB, nullable=False)
    triggered_value: Mapped[float | None] = mapped_column(Numeric(18, 8))
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class TradeJournalReview(Base):
    """ROB-405 Slice B — verdict (good/neutral/bad) for a trade_journal.

    Separate from TradeReview (which FKs review.trades). auto verdicts come from
    pnl_pct thresholds on closed mock journals; manual verdicts are overrides.
    """

    __tablename__ = "trade_journal_reviews"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('good','neutral','bad')",
            name="ck_trade_journal_reviews_verdict",
        ),
        CheckConstraint(
            "verdict_source IN ('auto','manual')",
            name="ck_trade_journal_reviews_source",
        ),
        Index("ix_trade_journal_reviews_journal_id", "journal_id"),
        Index(
            "uq_trade_journal_reviews_auto",
            "journal_id",
            unique=True,
            postgresql_where=text("verdict_source = 'auto'"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    journal_id: Mapped[int] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_source: Mapped[str] = mapped_column(Text, nullable=False)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class TradeJournalCounterfactual(Base):
    """ROB-405 Slice C — trigger vs actual fill vs no-action price for a
    watch-driven mock roundtrip. Quantifies the rule's effect. One row per
    correlation_id (idempotent)."""

    __tablename__ = "trade_journal_counterfactuals"
    __table_args__ = (
        UniqueConstraint(
            "correlation_id", name="uq_trade_journal_counterfactuals_correlation_id"
        ),
        Index("ix_trade_journal_counterfactuals_journal_id", "journal_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    journal_id: Mapped[int] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="CASCADE"), nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    triggered_value: Mapped[float | None] = mapped_column(Numeric(20, 8))
    actual_fill_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    no_action_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    no_action_as_of: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    fill_vs_trigger_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    no_action_vs_fill_pct: Mapped[float | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class TradeRetrospective(Base):
    """ROB-474 — structured trade retrospective (outcome + lesson + next strategy).

    Journal-side typed home for retro outcome so investment_report_items keeps its
    'no execution state on items' invariant. correlation_id is the idempotency key
    (NULL => ad-hoc append; set => upsert). journal_id uses SET NULL: a retro is a
    durable learning record that should survive journal deletion (deliberate
    deviation from the CASCADE used by sibling review tables).
    """

    __tablename__ = "trade_retrospectives"
    __table_args__ = (
        UniqueConstraint(
            "correlation_id", name="uq_trade_retrospectives_correlation_id"
        ),
        CheckConstraint(
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','alpaca_paper','upbit_live')",
            name="ck_trade_retrospectives_account_mode",
        ),
        CheckConstraint(
            "outcome IN ('filled','partially_filled','unfilled','rejected','cancelled')",
            name="ck_trade_retrospectives_outcome",
        ),
        CheckConstraint(
            "side IS NULL OR side IN ('buy','sell')",
            name="ck_trade_retrospectives_side",
        ),
        CheckConstraint(
            "realized_pnl_currency IS NULL OR realized_pnl_currency IN ('KRW','USD')",
            name="ck_trade_retrospectives_currency",
        ),
        CheckConstraint(
            "realized_pnl_source IS NULL OR "
            "realized_pnl_source IN ('caller_supplied','derived_from_journal')",
            name="ck_trade_retrospectives_pnl_source",
        ),
        Index("ix_trade_retrospectives_correlation_id", "correlation_id"),
        Index("ix_trade_retrospectives_journal_id", "journal_id"),
        Index("ix_trade_retrospectives_strategy_key", "strategy_key"),
        Index("ix_trade_retrospectives_symbol", "symbol"),
        Index("ix_trade_retrospectives_report_uuid", "report_uuid"),
        Index(
            "ix_trade_retrospectives_account_mode_created",
            "account_mode",
            "created_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    journal_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.trade_journals.id", ondelete="SET NULL")
    )
    report_uuid: Mapped[str | None] = mapped_column(Text)
    report_item_uuid: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str | None] = mapped_column(Text)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str | None] = mapped_column(Text)
    strategy_key: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    plan_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    realized_pnl_currency: Mapped[str | None] = mapped_column(Text)
    realized_pnl_source: Mapped[str | None] = mapped_column(Text)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    fill_evidence_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    lesson: Mapped[str | None] = mapped_column(Text)
    next_strategy: Mapped[str | None] = mapped_column(Text)
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    created_by_profile: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
