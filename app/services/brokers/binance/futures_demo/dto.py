"""ROB-298 PR 2 — DTOs for Futures Demo execution backend responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoPositionSideUnavailable,
)


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
    # ROB-1288: the broker's echoed ``positionSide``, preserved verbatim.
    # ``None`` means Binance did not send the field — never a stand-in for a
    # value inferred from ``side`` or from the sign of a quantity.
    position_side: str | None = None


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
    # ROB-1288: broker-echoed ``positionSide``, preserved verbatim (see
    # ``FuturesDemoOrderSubmitResult.position_side``).
    position_side: str | None = None


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
    # ROB-1289: broker-echoed ``positionSide``, preserved verbatim.  Absence
    # remains ``None``; callers must not infer it from any other field.
    position_side: str | None = None


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
    # ROB-1288: the row's ``positionSide`` as Binance sent it ("BOTH" in
    # One-way mode; "LONG"/"SHORT" under Hedge). ``None`` means the field was
    # absent from the row — 🔴 NOT "unknown, so assume from the sign of
    # ``position_amt``". Contract v2 §4.3 forbids that inference; callers that
    # need a value call :meth:`require_position_side` and get an exception.
    position_side: str | None = None

    def require_position_side(self) -> str:
        """Return the broker-reported ``positionSide`` or fail closed.

        🔴 There is deliberately no fallback. ``position_amt`` carries a sign
        that *looks* like it answers the question, and using it is exactly what
        contract v2 §4.3 prohibits ("v2 does not infer the missing value from
        quantity sign"). An absent value raises
        :class:`BinanceFuturesDemoPositionSideUnavailable` instead.
        """
        if self.position_side is None or not self.position_side.strip():
            raise BinanceFuturesDemoPositionSideUnavailable(
                "Futures Demo positionRisk row for symbol="
                f"{self.symbol!r} carries no positionSide. Refusing to infer it "
                "(position_amt sign is not evidence of positionSide; contract "
                "v2 §4.3)."
            )
        return self.position_side


@dataclass(frozen=True)
class FuturesDemoLeverageResult:
    symbol: str
    leverage: int  # echoed by Binance after set_leverage
    max_notional_value: Decimal


@dataclass(frozen=True)
class FuturesDemoPositionModeResult:
    is_hedge_mode: bool  # True = dual-side, False = One-way (required for PR 2)
