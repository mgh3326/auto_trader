"""Signal, volatility and sizing rules of the ROB-3796 exploration.

Times are epoch milliseconds. A 4h bar keyed by its open time ``o`` closes at
``o + BAR_MS``; a decision at close time ``T`` reads the bar opened at
``T - BAR_MS``. Any value whose inputs are missing returns ``None``, which the
simulator reads as NO_SIGNAL (never as zero, never forward-filled).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

HOUR_MS = 3_600_000
BAR_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
EWMA_DAYS = 20
EWMA_LAMBDA = 1 - 2 / (EWMA_DAYS + 1)  # span-20 smoothing factor
SIGMA_STAR = 0.60  # operator-fixed target annual volatility (design Q2)
DAYS_PER_YEAR = 365


def last_completed_day_open(decision_close_ms: int) -> int:
    """Open time of the newest UTC daily bar whose close is <= the decision."""
    return (decision_close_ms - DAY_MS) // DAY_MS * DAY_MS


def ewma_annual_vol(
    daily_closes: Mapping[int, float], last_day_open: int
) -> float | None:
    """Annualised 20-day EWMA volatility of daily simple returns.

    Uses the ``EWMA_DAYS`` most recent returns ending on ``last_day_open``; each
    return needs both of its daily closes. Missing closes, or zero variance,
    give ``None``.
    """
    days = [last_day_open - i * DAY_MS for i in range(EWMA_DAYS + 1)]
    if any(d not in daily_closes for d in days):
        return None
    weights = [EWMA_LAMBDA**i for i in range(EWMA_DAYS)]  # i=0 is most recent
    numerator = 0.0
    for i, weight in enumerate(weights):
        ret = daily_closes[days[i]] / daily_closes[days[i + 1]] - 1.0
        numerator += weight * ret * ret
    variance = numerator / sum(weights)
    if not math.isfinite(variance) or variance <= 0:
        return None
    return math.sqrt(variance * DAYS_PER_YEAR)


def momentum_sign(
    closes_4h: Mapping[int, float], decision_close_ms: int, lookback_days: int
) -> int | None:
    """Sign of ``close_T / close_(T - L days) - 1`` on 4h closes at the same
    time of day. ``None`` when either bar is missing, ``0`` for a flat return.
    """
    now_open = decision_close_ms - BAR_MS
    past_open = now_open - lookback_days * DAY_MS
    if now_open not in closes_4h or past_open not in closes_4h:
        return None
    ret = closes_4h[now_open] / closes_4h[past_open] - 1.0
    if ret > 0:
        return 1
    if ret < 0:
        return -1
    return 0


def vol_scale(sigma_annual: float) -> float:
    """``min(1, sigma_star / sigma_hat)``: scaling never exceeds the cap."""
    return min(1.0, SIGMA_STAR / sigma_annual)


def entry_notional(cap_usd: float, sigma_annual: float) -> float:
    return cap_usd * vol_scale(sigma_annual)
