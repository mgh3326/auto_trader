"""Pure US new-buy candidate quality gates + priority (ROB-346). No I/O.

Units: change_rate / week_change_rate are PERCENT (10.0 == +10%); latest_close
is USD; daily_volume is shares. Conservative thresholds (spec §3.3).
"""

from __future__ import annotations

import math
from typing import Any

PENNY_PRICE_USD = 5.0
ILLIQUID_DOLLAR_VOLUME_USD = 5_000_000.0
ABNORMAL_DAY_CHANGE_PCT = 15.0
ABNORMAL_WEEK_CHANGE_PCT = 50.0
STALE_CONFIDENCE_CAP = 40


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollar_volume_usd(latest_close: Any, daily_volume: Any) -> float | None:
    close = _f(latest_close)
    vol = _f(daily_volume)
    if close is None or vol is None:
        return None
    return close * vol


def compute_quality_flags(
    *,
    latest_close: Any,
    daily_volume: Any,
    change_rate: Any,
    week_change_rate: Any,
    is_common_stock: bool | None,
    screener_stale: bool,
) -> frozenset[str]:
    flags: set[str] = set()
    if is_common_stock is False:
        flags.add("non_common_stock")
    elif is_common_stock is None:
        flags.add("common_stock_unknown")
    close = _f(latest_close)
    if close is not None and close < PENNY_PRICE_USD:
        flags.add("penny")
    dv = dollar_volume_usd(latest_close, daily_volume)
    if dv is not None and dv < ILLIQUID_DOLLAR_VOLUME_USD:
        flags.add("illiquid")
    cr = _f(change_rate)
    wcr = _f(week_change_rate)
    if (cr is not None and cr > ABNORMAL_DAY_CHANGE_PCT) or (
        wcr is not None and wcr > ABNORMAL_WEEK_CHANGE_PCT
    ):
        flags.add("abnormal_spike")
    if screener_stale:
        flags.add("screener_stale")
    return frozenset(flags)


def compute_priority_score(
    *,
    latest_close: Any,
    daily_volume: Any,
    change_rate: Any,
    quality_flags: frozenset[str],
) -> float:
    dv = dollar_volume_usd(latest_close, daily_volume) or 0.0
    liquidity_term = min(1.0, math.log10(max(dv, 1.0)) / 9.0)  # 9 ≈ log10($1B)
    cr = _f(change_rate) or 0.0
    momentum_term = max(-5.0, min(10.0, cr)) / 10.0
    spike_penalty = 1.0 if "abnormal_spike" in quality_flags else 0.0
    stale_penalty = 1.0 if "screener_stale" in quality_flags else 0.0
    return (
        1.0 * liquidity_term
        + 0.5 * momentum_term
        - 0.5 * spike_penalty
        - 0.3 * stale_penalty
    )


def confidence_cap_for(quality_flags: frozenset[str]) -> int | None:
    if "screener_stale" in quality_flags or "common_stock_unknown" in quality_flags:
        return STALE_CONFIDENCE_CAP
    return None
