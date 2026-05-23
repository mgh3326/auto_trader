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
    floored_qty = (raw_qty / step_size).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * step_size
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


@dataclass(frozen=True)
class CloseQtyResult:
    """A sellable close quantity: step-floored, notional >= min_notional."""

    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class CloseQtyDust:
    """Free balance is non-zero but too small to place a min-notional SELL."""

    free: Decimal
    notional_usdt: Decimal
    reason: str


@dataclass(frozen=True)
class CloseResidualOutcome:
    kind: str  # "dust" | "anomaly"
    remediation_hint: str | None = None


def compute_close_qty(
    *,
    free_balance: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
) -> CloseQtyResult | CloseQtyDust:
    """Largest step-floored qty of the FREE balance whose notional clears
    ``min_notional``. Never reuses the original BUY qty; never rounds up."""
    if price <= 0:
        raise ValueError("price must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")
    floored = (free_balance / step_size).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * step_size
    notional = floored * price
    if floored <= 0:
        return CloseQtyDust(
            free=free_balance,
            notional_usdt=free_balance * price,
            reason=f"free={free_balance} below step_size={step_size}",
        )
    if notional < min_notional:
        return CloseQtyDust(
            free=free_balance,
            notional_usdt=notional,
            reason=f"closeable notional={notional} < MIN_NOTIONAL={min_notional}",
        )
    return CloseQtyResult(qty=floored, notional_usdt=notional)


def classify_close_residual(
    *,
    free_after: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    open_orders_empty: bool,
) -> CloseResidualOutcome:
    """Decide whether what is left after a close is benign dust or an anomaly.

    Dust (benign, -> ``reconciled`` with note) requires BOTH: the order book
    is clean AND no sellable (>= min_notional) chunk remains. Anything else
    is an anomaly carrying an operator-readable remediation hint."""
    leftover = compute_close_qty(
        free_balance=free_after,
        price=price,
        min_notional=min_notional,
        step_size=step_size,
    )
    if open_orders_empty and isinstance(leftover, CloseQtyDust):
        return CloseResidualOutcome(kind="dust")
    if not open_orders_empty:
        hint = (
            "Open orders remain after close. Cancel residual open orders, "
            "then re-run --confirm or remediate manually."
        )
    else:
        hint = (
            f"Sellable residual ~{leftover.notional_usdt} USDT (>= MIN_NOTIONAL "
            f"{min_notional}) left after close. Place a fee-adjusted MARKET SELL "
            "of the free base asset to flatten, then re-reconcile."
        )
    return CloseResidualOutcome(kind="anomaly", remediation_hint=hint)
