"""Volatility-targeted entry sizing; pure, never above the locked leg cap."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from .constants import LOCKED_LIMITS, SIGMA_STAR

_RESERVE_SCALE = Decimal("1e-12")


class TrendSizingBlocked(ValueError):
    pass


@dataclass(frozen=True)
class TrendSize:
    qty: Decimal
    #: ``qty * executable_price`` rounded UP to the 12 decimals the state stores,
    #: so a reserved notional can never be below the exposure it covers.
    reserved_notional_usdt: Decimal
    target_notional_usdt: Decimal


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0 or not step.is_finite() or step <= 0:
        raise TrendSizingBlocked("quantity and step must be finite and positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def target_notional(sigma_hat: Decimal) -> Decimal:
    """``cap * min(1, sigma* / sigma_hat)``; there is no scale-up above the cap."""
    if not sigma_hat.is_finite() or sigma_hat <= 0:
        raise TrendSizingBlocked("volatility estimate must be finite and positive")
    cap = LOCKED_LIMITS.leg_notional_cap_usdt
    return cap * min(Decimal(1), SIGMA_STAR / sigma_hat)


def size_entry(
    *,
    sigma_hat: Decimal,
    executable_price: Decimal,
    step_size: Decimal,
    min_notional_usdt: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
    quantity_precision: int,
) -> TrendSize:
    """Floor at lot step and quantity precision; never round up to pass a minimum."""
    values = (executable_price, step_size, min_notional_usdt, min_qty, max_qty)
    if any(not x.is_finite() or x <= 0 for x in values):
        raise TrendSizingBlocked("sizing inputs must be finite and positive")
    if not 0 <= quantity_precision <= 12 or min_qty > max_qty:
        raise TrendSizingBlocked("invalid exchange filters")
    target = target_notional(sigma_hat)
    qty = floor_to_step(target / executable_price, step_size)
    qty = qty.quantize(Decimal(1).scaleb(-quantity_precision), rounding=ROUND_DOWN)
    qty = floor_to_step(qty, step_size)
    notional = qty * executable_price
    if qty < min_qty or qty > max_qty or notional < min_notional_usdt:
        raise TrendSizingBlocked("floor quantity cannot satisfy exchange minimums")
    if notional > target:
        raise TrendSizingBlocked("floor quantity exceeds the volatility target")
    reserved = notional.quantize(_RESERVE_SCALE, rounding=ROUND_CEILING)
    if reserved > LOCKED_LIMITS.leg_notional_cap_usdt:
        raise TrendSizingBlocked("reserved notional exceeds the locked leg cap")
    return TrendSize(
        qty=qty, reserved_notional_usdt=reserved, target_notional_usdt=target
    )
