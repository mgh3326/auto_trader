"""ROB-298 PR 2 — DTOs for Futures Demo execution backend responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FuturesDemoOrderSubmitResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    status: str  # FILLED / PARTIALLY_FILLED / NEW / ...
    reduce_only: bool
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FuturesDemoOrderStatusResult:
    """Single-order status snapshot from a signed ``GET /fapi/v1/order``.

    ROB-305 §4: used to reconcile a submit response of ``status=NEW`` — the
    smoke polls this endpoint (bounded) to learn whether the order actually
    ``FILLED`` before the ledger is advanced past ``submitted``.
    """

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str  # FILLED / PARTIALLY_FILLED / NEW / CANCELED / REJECTED / ...
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    reduce_only: bool
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FuturesDemoOrderTestResult:
    """``/fapi/v1/order/test`` returned 200 with empty body."""

    symbol: str
    side: str
    order_type: str
    qty: Decimal


@dataclass(frozen=True)
class FuturesDemoCancelResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    status: str
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FuturesDemoOpenOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    status: str
    reduce_only: bool


@dataclass(frozen=True)
class FuturesDemoOpenOrdersResult:
    orders: list[FuturesDemoOpenOrder]


@dataclass(frozen=True)
class FuturesDemoPositionResult:
    """Single-symbol position snapshot from ``/fapi/v2/positionRisk``."""

    symbol: str
    position_amt: Decimal  # signed; positive=long, negative=short, 0=flat
    entry_price: Decimal
    leverage: int
    is_flat: bool


@dataclass(frozen=True)
class FuturesDemoLeverageResult:
    symbol: str
    leverage: int  # echoed by Binance after set_leverage
    max_notional_value: Decimal


@dataclass(frozen=True)
class FuturesDemoPositionModeResult:
    is_hedge_mode: bool  # True = dual-side, False = One-way (required for PR 2)
