"""ROB-353 (PR2) — pure analytics for the empirical RUN (no I/O, no network).

Baselines and robustness numbers the verdict report cites: weekly rebalance grid,
max drawdown, buy&hold return, and the survivorship-/quality-aware universe filter
(membership overlap + manifest coverage/confidence). Dollar-volume liquidity
filtering is intentionally NOT done here (disclosed as a skipped control).
"""
from __future__ import annotations

from collections.abc import Sequence

from pit_universe import PITManifest

_DAY_MS = 86_400_000


def weekly_rebalances(lo_ts: int, hi_ts: int, step_days: int = 7) -> list[int]:
    step = step_days * _DAY_MS
    return list(range(lo_ts, hi_ts + 1, step))


def buy_hold_bps(close_series: Sequence[tuple[int, float]]) -> float:
    if len(close_series) < 2:
        return 0.0
    first, last = close_series[0][1], close_series[-1][1]
    return (last - first) / first * 1e4 if first else 0.0


def max_drawdown_bps(period_net_pnls: Sequence[float], notional: float = 1000.0) -> float:
    equity = notional
    peak = notional
    worst = 0.0
    for pnl in period_net_pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak * 1e4)
    return worst


def filter_universe(manifest: PITManifest, lo_ts: int, hi_ts: int,
                    min_coverage: float = 0.8, confidences=("high", "medium")) -> list[str]:
    """Symbols whose listing overlaps [lo_ts, hi_ts] with adequate data quality."""
    kept = []
    for x in manifest.listings:
        overlaps = x.listed_from <= hi_ts and (x.delisted_at is None or x.delisted_at > lo_ts)
        cov_ok = (x.kline_coverage or 0.0) >= min_coverage
        conf_ok = x.confidence in confidences
        if overlaps and cov_ok and conf_ok:
            kept.append(x.symbol)
    return sorted(kept)
