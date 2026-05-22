"""ROB-298 — Spot Demo order sizing helper.

Computes ``qty`` from a target USDT notional under Binance Spot
exchangeInfo filters (``LOT_SIZE.stepSize`` + ``MIN_NOTIONAL``) and a
ROB-298 max cap. Always floors to step; never rounds up. If floored
quantity violates ``MIN_NOTIONAL``, returns ``SizingBlocked``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class SizingBlocked:
    reason: str


def compute_demo_order_qty(
    *,
    target_notional_usdt: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    cap_usdt: Decimal,
) -> SizingResult | SizingBlocked:
    if cap_usdt <= 0:
        raise ValueError("cap_usdt must be > 0")
    if price <= 0:
        raise ValueError("price must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    effective_target = min(target_notional_usdt, cap_usdt)
    raw_qty = effective_target / price
    floored_qty = (raw_qty / step_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_size
    if floored_qty <= 0:
        return SizingBlocked(
            reason=(
                f"floored qty=0 < MIN_NOTIONAL={min_notional} "
                f"(target={effective_target} / price={price} < step_size={step_size})"
            )
        )
    notional = floored_qty * price
    if notional < min_notional:
        return SizingBlocked(
            reason=f"notional={notional} < MIN_NOTIONAL={min_notional} after LOT_SIZE floor (qty={floored_qty})"
        )
    if notional > cap_usdt:
        # Defense in depth: floor should never go above cap; trip an
        # assertion-equivalent guard rather than silently send.
        return SizingBlocked(
            reason=f"computed notional={notional} > cap={cap_usdt} (sizing bug)"
        )
    return SizingResult(qty=floored_qty, notional_usdt=notional)
