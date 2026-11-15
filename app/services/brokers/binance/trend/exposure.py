"""Compare the trend lane with complete Futures Demo account truth; pure.

The demo account is shared with other strategies (ROB-1474 shorts, manual
smoke). For one signal symbol, anything on the broker that this lane does not
hold itself is foreign. Incomplete or ambiguous account data raises: it is
never read as "flat".
"""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOpenOrdersResult,
    FuturesDemoPositionResult,
)


class TrendExposureUnavailable(RuntimeError):
    """The account snapshot is incomplete or contradictory."""


class TrendExposureMismatch(RuntimeError):
    """A held symbol's broker position does not match what the lane holds."""


class ConflictReason:
    FOREIGN_POSITION = "foreign_position"
    FOREIGN_OPEN_ORDER = "foreign_open_order"
    POSITION_NOT_ONE_WAY_1X = "position_not_one_way_1x"


def validate_snapshot(
    positions: object, open_orders: object
) -> tuple[list[FuturesDemoPositionResult], FuturesDemoOpenOrdersResult]:
    if not isinstance(positions, list) or not isinstance(
        open_orders, FuturesDemoOpenOrdersResult
    ):
        raise TrendExposureUnavailable("complete broker account truth unavailable")
    seen: set[str] = set()
    for row in positions:
        if row.symbol in seen:
            raise TrendExposureUnavailable("duplicate broker position rows")
        seen.add(row.symbol)
    return positions, open_orders


def position_amount(positions: list[FuturesDemoPositionResult], symbol: str) -> Decimal:
    matches = [row for row in positions if row.symbol == symbol]
    if len(matches) > 1:
        raise TrendExposureUnavailable("duplicate broker position rows")
    return matches[0].position_amt if matches else Decimal(0)


def entry_conflicts(
    *,
    symbol: str,
    positions: object,
    open_orders: object,
) -> tuple[str, ...]:
    """Foreign exposure on ``symbol`` that blocks a new entry (empty = clear).

    At entry the lane holds nothing on the symbol (the state gate refuses
    otherwise), so any position or open order there is foreign.
    """
    rows, orders = validate_snapshot(positions, open_orders)
    reasons: list[str] = []
    for row in rows:
        if row.symbol != symbol:
            continue
        if row.position_amt != 0:
            reasons.append(ConflictReason.FOREIGN_POSITION)
            if row.position_side != "BOTH" or row.leverage != 1:
                reasons.append(ConflictReason.POSITION_NOT_ONE_WAY_1X)
    if any(order.symbol == symbol for order in orders.orders):
        reasons.append(ConflictReason.FOREIGN_OPEN_ORDER)
    return tuple(reasons)


def exit_quantity(
    *,
    symbol: str,
    side: str,
    expected_remaining: Decimal,
    positions: object,
    open_orders: object,
) -> Decimal:
    """Quantity a reduceOnly exit may close: the lane remainder, broker-proven.

    Refuses when the broker position is flat, on the other side, or not exactly
    the lane remainder (the lane and the broker disagree about what is held), or
    when an open order sits on the symbol.
    """
    rows, orders = validate_snapshot(positions, open_orders)
    if any(order.symbol == symbol for order in orders.orders):
        raise TrendExposureMismatch("open order on a held symbol")
    amount = position_amount(rows, symbol)
    signed = amount if side == "BUY" else -amount
    if signed <= 0:
        raise TrendExposureMismatch("broker position does not carry the held side")
    for row in rows:
        if row.symbol == symbol and (row.position_side != "BOTH" or row.leverage != 1):
            raise TrendExposureMismatch("broker position not 1x one-way")
    if signed != expected_remaining:
        # The state can only reconcile the post-fill position against the lane
        # remainder; an exit that would leave a different position (manual
        # reduction or addition) is refused before the fence.
        raise TrendExposureMismatch("broker position differs from the lane remainder")
    return expected_remaining
