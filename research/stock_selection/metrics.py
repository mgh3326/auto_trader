"""One scorer for any daily model scores; missing selections stay visible."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .dataset import COST_BPS


def _mean(values) -> float | None:
    values = [float(v) for v in values if pd.notna(v)]
    return float(np.mean(values)) if values else None


def _complete_mean(values: np.ndarray) -> float | None:
    return float(values.mean()) if len(values) and np.isfinite(values).all() else None


def _ranks(values: np.ndarray) -> np.ndarray:
    # Average ranks for ties, identical to pandas rank(method="average").
    _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    return (np.cumsum(counts) - (counts - 1) / 2)[inverse]


def _aggregate(daily: pd.DataFrame) -> dict:
    ics = daily.ic.dropna()
    sd = float(ics.std(ddof=1)) if len(ics) > 1 else 0.0
    return {
        "days": len(daily),
        "ic_days": len(ics),
        "ic": _mean(ics),
        "icir": float(ics.mean() / sd) if sd > 1e-12 else None,
        **{
            name: _mean(daily[name])
            for name in (
                "decile_spread",
                "cost_adjusted_spread",
                "up_hit",
                "down_hit",
                "turnover",
                "outcome_coverage",
            )
        },
        "complete_spread_days": int(daily.decile_spread.notna().sum()),
    }


def score_daily(
    scores: pd.DataFrame,
    *,
    market: str,
    horizon: int,
    top_n: int = 5,
    relative: bool = False,
) -> dict:
    """Spearman = Pearson of average ranks, ICIR = mean / sample std (unannualized).

    Decile spread uses ceil(10% N) and >=10 names. Every vintage pays a full
    long+short round trip: 4 * assumed one-way cost, without netting. This is
    an h-day selection diagnostic, not an executable daily-rebalanced NAV.
    Turnover = .5 * L1 change of signed decile weights (gross exposure 2),
    including initial formation. Up/down hit rates use strictly +/- raw returns.
    """
    if top_n < 1 or horizon not in (1, 3, 5, 10):
        raise ValueError("positive top_n and supported horizon required")
    if scores.empty or scores.duplicated(["date", "symbol"]).any():
        raise ValueError("nonempty unique daily scores required")
    if not np.isfinite(scores.score.to_numpy(float)).all():
        raise ValueError("scores must be finite")
    target = f"{'relative_return' if relative else 'return'}_{horizon}"
    daily, previous = [], {}
    ordered = scores.sort_values(
        ["date", "score", "symbol"], ascending=[True, False, True]
    )
    for day, group in ordered.groupby("date", sort=True):
        count = len(group)
        if count < 2:
            raise ValueError("daily cross-section needs at least two names")
        n = min(top_n, count // 2)
        outcomes = group[target].to_numpy(float)
        values = group.score.to_numpy(float)
        raw = group[f"return_{horizon}"].to_numpy(float)
        observed = np.isfinite(outcomes)
        x, y = values[observed], outcomes[observed]
        ic = None
        if len(x) >= 3 and np.ptp(x) > 0 and np.ptp(y) > 0:
            ic = float(
                np.corrcoef(
                    _ranks(x),
                    _ranks(y),
                )[0, 1]
            )
        size = math.ceil(count / 10)
        names = group.symbol.to_numpy()
        weights = {
            **dict.fromkeys(names[:size], 1 / size),
            **dict.fromkeys(names[-size:], -1 / size),
        }
        turnover = (
            sum(
                abs(weights.get(k, 0) - previous.get(k, 0))
                for k in set(weights) | set(previous)
            )
            / 2
        )
        previous = weights
        high, low = _complete_mean(outcomes[:size]), _complete_mean(outcomes[-size:])
        spread = (
            high - low if count >= 10 and high is not None and low is not None else None
        )
        daily.append(
            {
                "date": pd.Timestamp(day).date().isoformat(),
                "year": str(pd.Timestamp(day).year),
                "regime": str(group.regime.iloc[0]),
                "ic": ic,
                "decile_spread": spread,
                "cost_adjusted_spread": spread - 4 * COST_BPS[market] / 10000
                if spread is not None
                else None,
                "up_hit": float((raw[:n] > 0).mean())
                if np.isfinite(raw[:n]).all()
                else None,
                "down_hit": float((raw[-n:] < 0).mean())
                if np.isfinite(raw[-n:]).all()
                else None,
                "turnover": turnover,
                "outcome_coverage": float(observed.mean()),
                "names": count,
            }
        )
    frame = pd.DataFrame(daily)
    return {
        "summary": _aggregate(frame),
        "by_year": {str(key): _aggregate(g) for key, g in frame.groupby("year")},
        "by_regime": {str(key): _aggregate(g) for key, g in frame.groupby("regime")},
        "daily": daily,
        "one_way_cost_bps": COST_BPS[market],
        "target": target,
    }
