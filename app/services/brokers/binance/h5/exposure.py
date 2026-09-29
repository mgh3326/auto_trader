"""Compare H5 durable holdings with complete Futures Demo account truth."""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOpenOrdersResult,
    FuturesDemoPositionResult,
)

from .state import H5SignalSnapshot, H5StateBlocked


def position_amount(positions: list[FuturesDemoPositionResult], symbol: str) -> Decimal:
    matches = [row for row in positions if row.symbol == symbol]
    if len(matches) > 1:
        raise H5StateBlocked("duplicate broker position rows")
    return matches[0].position_amt if matches else Decimal(0)


def assert_account_exposure(
    *,
    signals: tuple[H5SignalSnapshot, ...],
    positions: list[FuturesDemoPositionResult],
    open_orders: FuturesDemoOpenOrdersResult,
    for_entry: bool,
    closing_symbol: str | None = None,
) -> None:
    """Foreign exposure stops entry; a close must match its own exact quantity."""
    if not isinstance(positions, list) or not isinstance(
        open_orders, FuturesDemoOpenOrdersResult
    ):
        raise H5StateBlocked("complete broker account truth unavailable")
    if open_orders.orders:
        raise H5StateBlocked("account has open orders")
    expected: dict[str, Decimal] = {}
    for signal in signals:
        if signal.state == "uncertain":
            raise H5StateBlocked("uncertain H5 exposure unresolved")
        if signal.state == "holding":
            if signal.symbol in expected:
                raise H5StateBlocked("multiple H5 holdings on one symbol")
            remaining = signal.remaining_qty
            if remaining <= 0:
                raise H5StateBlocked("invalid H5 held quantity")
            expected[signal.symbol] = remaining if signal.side == "BUY" else -remaining
    seen: set[str] = set()
    for row in positions:
        if row.symbol in seen:
            raise H5StateBlocked("duplicate broker position rows")
        seen.add(row.symbol)
        if row.position_amt == 0:
            continue
        if row.position_side != "BOTH" or row.leverage != 1:
            if for_entry or row.symbol in expected or row.symbol == closing_symbol:
                raise H5StateBlocked("broker position not 1x one-way")
        if row.symbol not in expected or row.position_amt != expected[row.symbol]:
            if for_entry or row.symbol == closing_symbol:
                raise H5StateBlocked("foreign or mismatched broker exposure")
    for symbol, qty in expected.items():
        if position_amount(positions, symbol) != qty:
            raise H5StateBlocked("H5 holding lacks broker position evidence")
    if for_entry and len(expected) >= 2:
        raise H5StateBlocked("H5 global position cap reached")
