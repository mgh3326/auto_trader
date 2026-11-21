"""R60, RSI14 and the two signal families (design §4).

``C_k`` is the close of the daily candle that opened ``k`` days before the most
recent completed one (``C_0``). Any missing close among ``C_0..C_60`` makes the
market ``NO_SIGNAL`` for that day; nothing is forward-filled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from .constants import LOOKBACK_DAYS, PULLBACK_RSI_MAX, RSI_PERIOD

Family = Literal["trend", "pullback"]

REASON_OK = "ok"
REASON_MISSING_CLOSE = "missing_close"
REASON_INVALID_CLOSE = "invalid_close"
REASON_R60_NOT_POSITIVE = "r60_not_positive"
REASON_RSI_UNAVAILABLE = "rsi_unavailable"
REASON_RSI_ABOVE_MAX = "rsi_above_max"
REASON_NO_REBOUND = "no_rebound"

_PRECISION = 50


def close_window(
    closes: Mapping[date, Decimal], last_closed_day: date
) -> tuple[Decimal, ...] | None:
    """``(C_0, ..., C_60)`` newest first, or ``None`` when any day is missing."""
    window: list[Decimal] = []
    for k in range(LOOKBACK_DAYS + 1):
        value = closes.get(last_closed_day - timedelta(days=k))
        if value is None:
            return None
        window.append(value)
    return tuple(window)


def r60(window: tuple[Decimal, ...]) -> Decimal:
    """``C_0 / C_60 - 1``; the caller has already checked the closes are > 0."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return window[0] / window[LOOKBACK_DAYS] - 1


def rsi14(closes_oldest_first: tuple[Decimal, ...]) -> Decimal | None:
    """Wilder RSI of the last close, the same recurrence as ROB-1382.

    ``market_data_indicators._calculate_rsi_series`` computes pandas
    ``ewm(alpha=1/14, min_periods=14, adjust=False)`` over gains and losses in
    which the first difference (NaN) has been replaced by 0 by ``Series.where``;
    so both averages start at 0 on the first close and then follow
    ``avg = (1 - a) * avg + a * x`` for every later difference. A value needs 14
    observations (14 closes). ``None`` where that function gives NaN: too few
    closes, or ``avg_loss == 0`` (no losses at all).
    """
    if len(closes_oldest_first) < RSI_PERIOD:
        return None
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        alpha = Decimal(1) / RSI_PERIOD
        keep = 1 - alpha
        avg_gain = Decimal(0)
        avg_loss = Decimal(0)
        for previous, current in zip(
            closes_oldest_first[:-1], closes_oldest_first[1:], strict=True
        ):
            delta = current - previous
            gain = delta if delta > 0 else Decimal(0)
            loss = -delta if delta < 0 else Decimal(0)
            avg_gain = keep * avg_gain + alpha * gain
            avg_loss = keep * avg_loss + alpha * loss
        if avg_loss == 0:
            return None
        return Decimal(100) - Decimal(100) / (1 + avg_gain / avg_loss)


@dataclass(frozen=True)
class Signal:
    family: Family
    enter: bool
    reason_code: str
    r60: Decimal | None
    rsi14: Decimal | None


def _no(family: Family, reason: str, r=None, rsi=None) -> Signal:
    return Signal(family, False, reason, r, rsi)


def _validated(window: tuple[Decimal, ...] | None) -> str | None:
    if window is None or len(window) != LOOKBACK_DAYS + 1:
        return REASON_MISSING_CLOSE
    if any((not v.is_finite()) or v <= 0 for v in window):
        return REASON_INVALID_CLOSE
    return None


def evaluate_trend(window: tuple[Decimal, ...] | None) -> Signal:
    """(A) ``R60 > 0`` strictly; otherwise cash (long only)."""
    problem = _validated(window)
    if problem is not None or window is None:
        return _no("trend", problem or REASON_MISSING_CLOSE)
    r = r60(window)
    if r > 0:
        return Signal("trend", True, REASON_OK, r, None)
    return _no("trend", REASON_R60_NOT_POSITIVE, r)


def evaluate_pullback(window: tuple[Decimal, ...] | None) -> Signal:
    """(B) ``R60 > 0`` AND ``RSI14 <= 40`` AND ``C_0 > C_1``."""
    problem = _validated(window)
    if problem is not None or window is None:
        return _no("pullback", problem or REASON_MISSING_CLOSE)
    r = r60(window)
    if r <= 0:
        return _no("pullback", REASON_R60_NOT_POSITIVE, r)
    rsi = rsi14(tuple(reversed(window)))
    if rsi is None:
        return _no("pullback", REASON_RSI_UNAVAILABLE, r)
    if rsi > PULLBACK_RSI_MAX:
        return _no("pullback", REASON_RSI_ABOVE_MAX, r, rsi)
    if not window[0] > window[1]:
        return _no("pullback", REASON_NO_REBOUND, r, rsi)
    return Signal("pullback", True, REASON_OK, r, rsi)
