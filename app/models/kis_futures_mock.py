"""KIS 국내 선물 모의 주문 원장·귀속 신호 원장 (hk 5946, 설계 5939 §4).

``execution_ledger`` 는 선물을 담지 못하므로 베뉴별 분리 원장이다. 쓰기는 서비스
레이어(`app/services/kis_futures_mock/ledger_service.py`, `attribution.py`)만 한다.
T3 는 주문 행(``place``/``cancel``)만 쓴다 — 체결(T4)·대사(T5)가 쓰는 열
(``filled_qty``·``avg_fill_price``·``fee``·``realized_pnl_krw``·``reconciled_at``)은
nullable 로만 둔다. 포지션·정산 원장은 후속 티켓이다.

CHECK 는 짧은 이름을 쓰고, 명명 규칙이 ``ck_<table>_`` 를 붙인다(마이그레이션
``20261121_hk5946_futures_mock`` 이 ``op.f`` 로 같은 철자를 고정한다). 잠금 상수
(1계약·명목 50,000,000 KRW·지정가·닫힌 상품)는 DB CHECK 에도 반복한다 — 올리려면
마이그레이션이 필요하다.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

FUTURES_MOCK_ORDER_STATUSES = (
    "accepted",
    "rejected",
    "pending",
    "partial",
    "filled",
    "cancelled",
    "expired",
    "uncertain",
    "anomaly",
    "requires_manual_review",
)
FUTURES_MOCK_SIGNAL_OUTCOME_STATES = ("recorded", "submitted", "suppressed", "failed")
#: 한 상품 코드만 — 닫힌 집합과 같은 값 (constants.CLOSED_PRODUCTS).
FUTURES_MOCK_PRODUCT_CODES = ("MF200",)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class KISFuturesMockOrderLedger(Base):
    __tablename__ = "kis_futures_mock_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id",
            name="uq_kis_futures_mock_order_ledger_client_order_id",
        ),
        UniqueConstraint(
            "account_mode",
            "trading_day",
            "broker_order_id",
            name="uq_kis_futures_mock_order_ledger_account_day_broker_order",
        ),
        CheckConstraint("broker = 'kis'", name="broker"),
        CheckConstraint("account_mode = 'kis_futures_mock'", name="account_mode"),
        CheckConstraint(
            _in("product_code", FUTURES_MOCK_PRODUCT_CODES), name="product"
        ),
        CheckConstraint("currency = 'KRW'", name="currency"),
        CheckConstraint("operation_kind IN ('place','cancel')", name="operation_kind"),
        CheckConstraint("side IN ('buy','sell')", name="side"),
        CheckConstraint("order_type = 'limit'", name="limit_only"),
        CheckConstraint("position_effect IN ('open','close')", name="position_effect"),
        CheckConstraint("quantity > 0 AND quantity <= 1", name="quantity_locked"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint(
            "operation_kind <> 'place' OR amount <= 50000000",
            name="notional_locked",
        ),
        CheckConstraint(
            _in("status", FUTURES_MOCK_ORDER_STATUSES),
            name="status",
        ),
        CheckConstraint(
            "status <> 'accepted' OR broker_order_id IS NOT NULL",
            name="accepted_has_broker_order_id",
        ),
        CheckConstraint(
            "operation_kind = 'place' OR original_order_id IS NOT NULL",
            name="original_required_for_cancel",
        ),
        CheckConstraint(
            "length(btrim(client_order_id)) > 0", name="client_id_nonblank"
        ),
        Index("ix_kis_futures_mock_order_ledger_status", "status"),
        Index(
            "ix_kis_futures_mock_order_ledger_day_symbol",
            "trading_day",
            "contract_symbol",
        ),
        Index("ix_kis_futures_mock_order_ledger_correlation_id", "correlation_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_date: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    #: KRX 파생 주간 세션의 KST 달력일 (YYYYMMDD, 설계 §5).
    trading_day: Mapped[str] = mapped_column(Text, nullable=False)
    contract_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    broker_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    product_code: Mapped[str] = mapped_column(Text, nullable=False)
    contract_month: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    position_effect: Mapped[str] = mapped_column(Text, nullable=False)
    operation_kind: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    #: price × 승수 × 수량 (KRW).
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)

    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    original_order_id: Mapped[str | None] = mapped_column(Text)

    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    broker: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # T4/T5 가 쓰는 열 — T3 는 항상 NULL.
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    realized_pnl_krw: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    reconcile_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    strategy: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KISFuturesMockSignalLedger(Base):
    """송신 전 귀속 신호 — ``strategy``·``signal_source``·``correlation_id`` 는 비어 있으면
    INSERT 를 거부한다 (하드룰 7 의 선물판)."""

    __tablename__ = "kis_futures_mock_signal_ledger"
    __table_args__ = (
        UniqueConstraint(
            "correlation_id",
            name="uq_kis_futures_mock_signal_ledger_correlation_id",
        ),
        CheckConstraint("account_mode = 'kis_futures_mock'", name="account_mode"),
        CheckConstraint("decision IN ('order','no_order')", name="decision"),
        CheckConstraint(
            _in("outcome_state", FUTURES_MOCK_SIGNAL_OUTCOME_STATES),
            name="outcome_state",
        ),
        CheckConstraint("side IS NULL OR side IN ('buy','sell')", name="side"),
        CheckConstraint("length(btrim(strategy)) > 0", name="strategy_nonblank"),
        CheckConstraint("length(btrim(signal_source)) > 0", name="source_nonblank"),
        CheckConstraint(
            "length(btrim(correlation_id)) > 0", name="correlation_nonblank"
        ),
        Index("ix_kis_futures_mock_signal_ledger_kst_date", "kst_date"),
        Index("ix_kis_futures_mock_signal_ledger_strategy", "strategy"),
        Index("ix_kis_futures_mock_signal_ledger_outcome_state", "outcome_state"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    signal_source: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)

    contract_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    broker_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str | None] = mapped_column(Text)
    intended_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    intended_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    decision: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_state: Mapped[str] = mapped_column(Text, nullable=False, default="recorded")
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    kst_date: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
