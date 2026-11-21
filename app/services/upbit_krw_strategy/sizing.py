"""Volatility-targeted entry size (design §5.3). Pure, never above the cap."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext

from app.core.upbit_tick import is_on_upbit_krw_tick

from .constants import (
    ANNUALISATION_DAYS,
    EWMA_SPAN_DAYS,
    LEG_NOTIONAL_CAP_KRW,
    MIN_ENTRY_NOTIONAL_KRW,
    MIN_LAST_CLOSE_KRW,
    MIN_ORDER_KRW,
    QUANTITY_STEP,
    SIGMA_STAR,
)
from .types import require_decimal

_PRECISION = 50
SKIP_BELOW_FLOOR = "skipped_below_floor"
SKIP_BELOW_MIN_ORDER = "skipped_below_min_order"
SKIP_VOL_UNAVAILABLE = "skipped_vol_unavailable"


def realized_vol_annual(closes_newest_first: tuple[Decimal, ...]) -> Decimal | None:
    """EWMA (span 20) of daily simple returns, annualised by sqrt(365).

    Needs ``C_0..C_20`` (21 closes, 20 returns), newest first. Zero-mean second
    moment with weights ``(1 - alpha)^j`` newest first and ``alpha = 2/21``,
    the same definition as the TREND demo ``realized_vol_annual``. ``None``
    when a close is missing/not positive or the estimate is not positive.
    """
    needed = EWMA_SPAN_DAYS + 1
    if len(closes_newest_first) < needed:
        return None
    closes = closes_newest_first[:needed]
    if any((not c.is_finite()) or c <= 0 for c in closes):
        return None
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        alpha = Decimal(2) / Decimal(EWMA_SPAN_DAYS + 1)
        weight = Decimal(1)
        total_weight = Decimal(0)
        second_moment = Decimal(0)
        for newer, older in zip(closes[:-1], closes[1:], strict=True):
            daily = newer / older - 1
            second_moment += weight * daily * daily
            total_weight += weight
            weight *= 1 - alpha
        sigma = (second_moment / total_weight).sqrt() * Decimal(
            ANNUALISATION_DAYS
        ).sqrt()
    return sigma if sigma > 0 else None


def target_notional_krw(sigma_hat: Decimal) -> Decimal:
    """``480,000 * min(1, 0.60 / sigma_hat)``; there is no scale-up above the cap."""
    if not sigma_hat.is_finite() or sigma_hat <= 0:
        raise ValueError("sigma_hat must be a finite positive Decimal")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        # cap * min(1, sigma* / sigma_hat), with the product taken before the
        # division so an exact boundary (e.g. 50,000 KRW) is not lost to rounding.
        return min(LEG_NOTIONAL_CAP_KRW, LEG_NOTIONAL_CAP_KRW * SIGMA_STAR / sigma_hat)


@dataclass(frozen=True)
class EntrySize:
    sized: bool
    reason_code: str
    target_notional_krw: Decimal | None
    quantity: Decimal | None
    notional_krw: Decimal | None


def floor_quantity(value: Decimal) -> Decimal:
    """Floor to 8 decimals; never round up."""
    return value.quantize(QUANTITY_STEP, rounding=ROUND_DOWN)


def size_entry(*, sigma_hat: Decimal | None, limit_price_krw: Decimal) -> EntrySize:
    """Target notional, then ``floor8(N / limit_price)``.

    ``N < 50,000`` KRW skips the entry. The price must be an exact Decimal on
    the Upbit tick of its own range and at least 100 KRW (Go model limit).
    """
    require_decimal(limit_price_krw, "limit_price_krw")
    if limit_price_krw < MIN_LAST_CLOSE_KRW:
        raise ValueError("limit price must be a finite Decimal >= 100 KRW")
    if not is_on_upbit_krw_tick(limit_price_krw):
        raise ValueError("limit price is not on the Upbit tick")
    if sigma_hat is None:
        return EntrySize(False, SKIP_VOL_UNAVAILABLE, None, None, None)
    target = target_notional_krw(sigma_hat)
    if target < MIN_ENTRY_NOTIONAL_KRW:
        return EntrySize(False, SKIP_BELOW_FLOOR, target, None, None)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        quantity = floor_quantity(target / limit_price_krw)
        notional = quantity * limit_price_krw
    if quantity <= 0 or notional < MIN_ORDER_KRW:
        return EntrySize(False, SKIP_BELOW_MIN_ORDER, target, quantity, notional)
    return EntrySize(True, "ok", target, quantity, notional)
