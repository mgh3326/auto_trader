"""Metrics on a portfolio equity series marked at each 4h bar close."""

from __future__ import annotations

import math
from collections.abc import Sequence

BARS_PER_YEAR = 365 * 6  # 4h bars, 24/7 crypto calendar


def net_return(base_usd: float, equity: Sequence[float]) -> float:
    """Final equity against the capital base, after costs (open legs marked)."""
    return (equity[-1] - base_usd) / base_usd


def max_drawdown(base_usd: float, equity: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a fraction of the peak, base included."""
    peak = base_usd
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe(base_usd: float, equity: Sequence[float]) -> float | None:
    """Annualised Sharpe of 4h equity returns with a zero risk-free rate.

    ``None`` when the return series has no variance (for example cash).
    """
    series = [base_usd, *equity]
    rets = [b / a - 1.0 for a, b in zip(series, series[1:], strict=False) if a > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(BARS_PER_YEAR)
