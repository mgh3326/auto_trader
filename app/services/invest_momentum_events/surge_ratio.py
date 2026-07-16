"""ROB-919: relative trade-value surge-ratio calculation.

Pure computation only -- no DB/network access. Callers fetch the current and
historical same-time-of-day trade values (see repository.py) and pass them in
here. Historical entries that are ``None`` (the symbol had no observation at
that time on that day -- e.g. it wasn't in top-100 rankings yet) are dropped
before averaging rather than treated as zero, so a recently-listed symbol or
one with a trading-halt gap in its history degrades to a null ratio with an
explicit reason instead of a distorted number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_MIN_LOOKBACK_DAYS = 3


@dataclass(frozen=True)
class TradeValueSurgeRatio:
    ratio: float | None
    reason_code: str | None
    lookback_days_used: int
    baseline_trade_value: float | None


def compute_trade_value_surge_ratio(
    *,
    current_trade_value: Decimal | float | None,
    historical_trade_values: list[Decimal | float | None],
    min_lookback_days: int = DEFAULT_MIN_LOOKBACK_DAYS,
) -> TradeValueSurgeRatio:
    if current_trade_value is None:
        return TradeValueSurgeRatio(
            ratio=None,
            reason_code="missing_current_trade_value",
            lookback_days_used=0,
            baseline_trade_value=None,
        )

    usable = [float(value) for value in historical_trade_values if value is not None]
    if len(usable) < min_lookback_days:
        return TradeValueSurgeRatio(
            ratio=None,
            reason_code="insufficient_history",
            lookback_days_used=len(usable),
            baseline_trade_value=None,
        )

    baseline = sum(usable) / len(usable)
    if baseline <= 0:
        return TradeValueSurgeRatio(
            ratio=None,
            reason_code="zero_baseline_trade_value",
            lookback_days_used=len(usable),
            baseline_trade_value=baseline,
        )

    ratio = float(current_trade_value) / baseline
    return TradeValueSurgeRatio(
        ratio=round(ratio, 4),
        reason_code=None,
        lookback_days_used=len(usable),
        baseline_trade_value=round(baseline, 2),
    )
