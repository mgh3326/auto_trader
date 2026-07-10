"""Trade review system models (review schema)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
        Index("ix_kis_mock_ledger_report_item_uuid", "report_item_uuid"),
        Index(
            "ix_kis_mock_ledger_mirror_cohort_created", "mirror_cohort", "created_at"
        ),
        Index(
            "ux_kis_mock_mirror_report_item_once",
            "mirror_cohort",
            "report_item_uuid",
            unique=True,
            postgresql_where=text(
                "mirror_cohort = 'mock_counterfactual' AND report_item_uuid IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "mirror_cohort IS NULL OR mirror_cohort IN ('mock_counterfactual')",
            name="ck_kis_mock_ledger_mirror_cohort",
        ),
        CheckConstraint(
            "mirror_source_bucket IS NULL OR mirror_source_bucket IN "
            "('place_original','watch_trigger','deferred_min_rung')",
            name="ck_kis_mock_ledger_mirror_source_bucket",
        ),
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

    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    mirror_cohort: Mapped[str | None] = mapped_column(Text)
    mirror_source_bucket: Mapped[str | None] = mapped_column(Text)

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
        Index("ix_kis_live_ledger_report_item_uuid", "report_item_uuid"),
        # ROB-714 — learning-loop provenance spine join index.
        Index("ix_kis_live_ledger_correlation_id", "correlation_id"),
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
    exit_intent: Mapped[str | None] = mapped_column(Text)  # ROB-800: 'loss_cut'
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # ROB-473 — audit linkage to the report item that drove this order.
    # send-time, immutable through reconcile; NO FK (mirrors
    # AlpacaPaperOrderLedger.candidate_uuid). nullable: legacy/unlinked → NULL.
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # ROB-653 P6-B — content approval-hash + local idempotency key (additive).
    approval_hash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    # ROB-714 — learning-loop provenance spine (send-time mint, immutable).
    # Links this order to its place-time forecast + reconcile-time journal +
    # retrospective. NULL for legacy rows. See app.services.live_correlation.
    correlation_id: Mapped[str | None] = mapped_column(Text)

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
        Index("ix_live_ledger_report_item_uuid", "report_item_uuid"),
        # ROB-714 — learning-loop provenance spine join index.
        Index("ix_live_ledger_correlation_id", "correlation_id"),
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
    exit_intent: Mapped[str | None] = mapped_column(Text)  # ROB-800: 'loss_cut'
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # ROB-473 — audit linkage to the report item that drove this order (see
    # KISLiveOrderLedger.report_item_uuid). send-time, immutable, no FK.
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # ROB-653 P6-B — content approval-hash + local idempotency key (additive).
    approval_hash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    # ROB-714 — learning-loop provenance spine (send-time mint, immutable).
    # Links this order to its place-time forecast + reconcile-time journal +
    # retrospective. NULL for legacy rows. See app.services.live_correlation.
    correlation_id: Mapped[str | None] = mapped_column(Text)

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

    buy_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    sell_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    fx_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    total_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_rate_source: Mapped[str | None] = mapped_column(Text)
    fx_pnl_accuracy: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrderSendIntent(Base):
    """ROB-653 P6-B — KIS pre-send reservation for local double-send protection.

    KIS has no broker idempotency field, so a UNIQUE (account_scope,
    idempotency_key) row is inserted immediately before the order POST. A
    same-key insert the same trading day raises IntegrityError → fail-closed.
    Never read by reconcile; purely a send-time guard.
    """

    __tablename__ = "order_send_intents"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "idempotency_key",
            name="uq_order_send_intent_scope_key",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_scope: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class TossLiveOrderLedger(Base):
    """ROB-538 — Toss live order lifecycle ledger.

    Toss orders are recorded accepted-only at send time. Fills, journals, and
    realized PnL are booked only by toss_reconcile_orders from GET
    /orders/{orderId} evidence.
    """

    __tablename__ = "toss_live_order_ledger"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_toss_live_ledger_client_order_id"),
        UniqueConstraint("broker_order_id", name="uq_toss_live_ledger_broker_order_id"),
        CheckConstraint("broker = 'toss'", name="toss_live_ledger_broker_toss"),
        CheckConstraint(
            "account_mode = 'toss_live'",
            name="toss_live_ledger_account_mode_toss_live",
        ),
        CheckConstraint(
            "operation_kind IN ('place','modify','cancel')",
            name="toss_live_ledger_operation_kind",
        ),
        CheckConstraint("market IN ('kr','us')", name="toss_live_ledger_market"),
        CheckConstraint("side IN ('buy','sell')", name="toss_live_ledger_side"),
        CheckConstraint(
            "order_type IN ('limit','market')", name="toss_live_ledger_order_type"
        ),
        CheckConstraint(
            "status IN ("
            "'accepted','rejected','pending','partial','filled','cancelled',"
            "'replaced','cancel_rejected','replace_rejected','anomaly'"
            ")",
            name="toss_live_ledger_status",
        ),
        Index("ix_toss_live_ledger_status", "status"),
        Index("ix_toss_live_ledger_market_symbol", "market", "symbol"),
        Index("ix_toss_live_ledger_broker_status", "broker_status"),
        Index("ix_toss_live_ledger_report_item_uuid", "report_item_uuid"),
        Index("ix_toss_live_ledger_replaced_by", "replaced_by_order_id"),
        # ROB-714 — learning-loop provenance spine join index.
        Index("ix_toss_live_ledger_correlation_id", "correlation_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    broker: Mapped[str] = mapped_column(Text, nullable=False, default="toss")
    account_mode: Mapped[str] = mapped_column(Text, nullable=False, default="toss_live")
    operation_kind: Mapped[str] = mapped_column(Text, nullable=False)

    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    time_in_force: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    order_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(Text)

    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    original_order_id: Mapped[str | None] = mapped_column(Text)
    replaced_by_order_id: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    broker_status: Mapped[str | None] = mapped_column(Text)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    requires_manual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    manual_review_reason: Mapped[str | None] = mapped_column(Text)
    last_reconcile_error: Mapped[dict | None] = mapped_column(JSONB)

    reason: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    approval_hash: Mapped[str | None] = mapped_column(Text)
    # ROB-714 — learning-loop provenance spine (send-time mint, immutable).
    # Links this order to its place-time forecast + reconcile-time journal +
    # retrospective. NULL for legacy rows. See app.services.live_correlation.
    correlation_id: Mapped[str | None] = mapped_column(Text)

    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    settlement_date: Mapped[date | None] = mapped_column(Date)
    trade_id: Mapped[int | None] = mapped_column(BigInteger)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    buy_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    sell_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    fx_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    total_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_rate_source: Mapped[str | None] = mapped_column(Text)
    fx_pnl_accuracy: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TossFillPollState(Base):
    """ROB-757 state for Toss REST fill polling.

    One row per scan scope. The cursor is intentionally a timestamp, not Toss's
    opaque page cursor, because Toss cursors are page-local and not stable across
    scheduled runs.
    """

    __tablename__ = "toss_fill_poll_state"
    __table_args__ = ({"schema": "review"},)

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[dict | None] = mapped_column(JSONB)
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
            "correlation_id",
            "account_mode",
            name="uq_trade_retrospectives_correlation_account",
        ),
        CheckConstraint(
            "account_mode IN ('kis_mock','kiwoom_mock','kis_live','toss_live','alpaca_paper','upbit_live','paper')",
            name="account_mode",
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
        # ROB-647 — postmortem structuring. trigger_type is deliberately
        # separate from outcome (expired collapses to cancelled at the outcome
        # layer; see kis_live_ledger.py). Kept in lock-step with
        # app/schemas/trade_retrospective.py VALID_TRIGGER_TYPES.
        CheckConstraint(
            "trigger_type IS NULL OR trigger_type IN ("
            "'fill','partial_fill','rejected_order','cancelled','expired',"
            "'thesis_change','policy_violation','stale_evidence','guardrail_block','stop_loss'"
            ")",
            name="ck_trade_retrospectives_trigger_type",
        ),
        CheckConstraint(
            "root_cause_class IS NULL OR root_cause_class IN ("
            "'user_input','analysis','policy','execution','harness'"
            ")",
            name="ck_trade_retrospectives_root_cause_class",
        ),
        Index("ix_trade_retrospectives_correlation_id", "correlation_id"),
        Index("ix_trade_retrospectives_journal_id", "journal_id"),
        Index("ix_trade_retrospectives_strategy_key", "strategy_key"),
        Index("ix_trade_retrospectives_symbol", "symbol"),
        Index("ix_trade_retrospectives_report_uuid", "report_uuid"),
        Index("ix_trade_retrospectives_report_item_uuid", "report_item_uuid"),
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

    buy_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    sell_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    fx_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    security_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    total_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fx_rate_source: Mapped[str | None] = mapped_column(Text)
    fx_pnl_accuracy: Mapped[str | None] = mapped_column(Text)

    fill_evidence_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    lesson: Mapped[str | None] = mapped_column(Text)
    next_strategy: Mapped[str | None] = mapped_column(Text)
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    created_by_profile: Mapped[str | None] = mapped_column(Text)

    # ROB-647 — postmortem structuring (all additive nullable). JSONB fields are
    # validated through app/schemas/trade_retrospective.py before write.
    trigger_type: Mapped[str | None] = mapped_column(Text)
    root_cause_class: Mapped[str | None] = mapped_column(Text)
    intended_vs_happened: Mapped[dict | None] = mapped_column(JSONB)
    next_actions: Mapped[list | None] = mapped_column(JSONB)
    guardrail_fired: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# review.trade_forecasts — resolvable probabilistic forecasts (ROB-650)
# ---------------------------------------------------------------------------
class TradeForecast(Base):
    """ROB-650 — resolvable prediction ledger with deterministic scoring.

    A forecast records a probabilistic, resolvable claim made when composing a
    buy thesis or a profit-taking (WATCH→PLACE) verdict — e.g. "P(005930 touches
    129,600 support by 2026-07-15) = 0.65". Composition is a Claude session
    (LLM boundary); the record/resolve/score logic here is fully deterministic.

    ``forecast_id`` is the idempotency key (client-supplied to update while open,
    or auto-generated). ``forecast_target`` is a structured JSONB claim; the
    ``price_target`` kind resolves deterministically against loaded daily OHLCV
    (ROB-639), non-price kinds resolve via an operator-supplied manual outcome
    (evidence required). ``correlation_id`` aligns with trade_retrospectives
    (ROB-647) so a postmortem can cite the forecast it graded.
    """

    __tablename__ = "trade_forecasts"
    __table_args__ = (
        UniqueConstraint("forecast_id", name="uq_trade_forecasts_forecast_id"),
        CheckConstraint(
            "status IN ('open','closed')",
            name="ck_trade_forecasts_status",
        ),
        CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="ck_trade_forecasts_probability",
        ),
        CheckConstraint(
            "(probability_range_low IS NULL AND probability_range_high IS NULL) OR "
            "(probability_range_low IS NOT NULL "
            "AND probability_range_high IS NOT NULL "
            "AND probability_range_low <= probability_range_high "
            "AND probability >= probability_range_low "
            "AND probability <= probability_range_high)",
            name="ck_trade_forecasts_probability_range",
        ),
        CheckConstraint(
            "brier_score IS NULL OR (brier_score >= 0 AND brier_score <= 1)",
            name="ck_trade_forecasts_brier_score",
        ),
        Index("ix_trade_forecasts_status_review_date", "status", "review_date"),
        Index("ix_trade_forecasts_symbol", "symbol"),
        Index("ix_trade_forecasts_created_by", "created_by"),
        Index("ix_trade_forecasts_correlation_id", "correlation_id"),
        Index("ix_trade_forecasts_report_item_uuid", "report_item_uuid"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    forecast_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # Loose reference links (no FK — a forecast is a durable learning record
    # that outlives the artifact/journal/report it was born from).
    artifact_uuid: Mapped[str | None] = mapped_column(Text)
    journal_id: Mapped[int | None] = mapped_column(BigInteger)
    report_uuid: Mapped[str | None] = mapped_column(Text)
    report_item_uuid: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)

    # Attribution (calibration groups by these labels).
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    session_label: Mapped[str | None] = mapped_column(Text)
    model_label: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)

    # The claim.
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    forecast_target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    horizon: Mapped[str | None] = mapped_column(Text)
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    probability_range_low: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    probability_range_high: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    evidence_ids: Mapped[list | None] = mapped_column(JSONB)
    contrary_evidence: Mapped[str | None] = mapped_column(Text)
    resolution_source: Mapped[str | None] = mapped_column(Text)
    forecast_start_date: Mapped[date | None] = mapped_column(Date)
    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )

    # Deterministic resolution outputs (populated at resolve time).
    outcome: Mapped[bool | None] = mapped_column(Boolean)
    observed_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    brier_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    resolution_detail: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
