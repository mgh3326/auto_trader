"""H5-only 1x isolated position sizing; no ROB-993 or DFC caps are changed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

RISK_FRACTION = Decimal("0.01")
HARD_STOP_FRACTION = Decimal("0.05")
NOTIONAL_FRACTION = RISK_FRACTION / HARD_STOP_FRACTION


class H5SizingBlocked(ValueError):
    pass


@dataclass(frozen=True)
class H5Size:
    qty: Decimal
    notional_usdt: Decimal
    maximum_notional_usdt: Decimal


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0 or not step.is_finite() or step <= 0:
        raise H5SizingBlocked("quantity and step must be finite and positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def size_entry(
    *,
    nav_usdt: Decimal,
    executable_price: Decimal,
    step_size: Decimal,
    min_notional_usdt: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
    quantity_precision: int,
) -> H5Size:
    """Floor at both lot step and quantity precision; never round up to pass a min."""
    values = (
        nav_usdt,
        executable_price,
        step_size,
        min_notional_usdt,
        min_qty,
        max_qty,
    )
    if any(not x.is_finite() or x <= 0 for x in values):
        raise H5SizingBlocked("H5 sizing inputs must be finite and positive")
    if not 0 <= quantity_precision <= 12 or min_qty > max_qty:
        raise H5SizingBlocked("invalid H5 exchange filters")
    cap = nav_usdt * NOTIONAL_FRACTION
    qty = floor_to_step(cap / executable_price, step_size)
    quantum = Decimal(1).scaleb(-quantity_precision)
    qty = qty.quantize(quantum, rounding=ROUND_DOWN)
    qty = floor_to_step(qty, step_size)
    notional = qty * executable_price
    if qty < min_qty or qty > max_qty or notional < min_notional_usdt:
        raise H5SizingBlocked("floor quantity cannot satisfy exchange minimums")
    if notional > cap:
        raise H5SizingBlocked("floor quantity exceeds NAV risk cap")
    return H5Size(qty=qty, notional_usdt=notional, maximum_notional_usdt=cap)
