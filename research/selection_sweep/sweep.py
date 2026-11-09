"""Per-date strategy and control series, then per-cell statistics.

Controls are computed from the same eligible names, the same dates, the same
horizon and the same per-side cost as every ranker: seeded random selection of
the same decile size (200 seeds), the equal-weight eligible universe, and the
benchmark held over the same open-to-open interval.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import spec
from .panel import Panel, eligibility, features, outcomes


@dataclass
class HorizonSeries:
    dates: pd.DatetimeIndex  # decision dates (valid ones)
    universe: np.ndarray  # eligible names N per date
    size: np.ndarray  # decile size n per date
    ew: np.ndarray
    bench: np.ndarray
    unknown_share: np.ndarray  # share of eligible names with unknown outcome
    rand_long: np.ndarray  # (T, S)
    rand_short: np.ndarray  # (T, S)
    long: dict[str, np.ndarray] = field(default_factory=dict)
    short: dict[str, np.ndarray] = field(default_factory=dict)


def _known_mean(values: np.ndarray, axis: int = -1) -> np.ndarray:
    """Mean of known outcomes; NaN when fewer than MIN_KNOWN_SHARE are known."""
    known = np.isfinite(values)
    count = known.sum(axis=axis)
    total = np.where(known, values, 0.0).sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = total / count
    share = count / values.shape[axis]
    return np.where(share >= spec.MIN_KNOWN_SHARE, mean, np.nan)


def decile_size(n_names: int) -> int:
    return max(1, math.ceil(spec.DECILE * n_names))


def run_market(
    panel: Panel,
    window: tuple[str, str],
    horizons=spec.HORIZONS,
    rankers=spec.RANKERS,
    seeds=spec.SEEDS,
) -> dict[int, HorizonSeries]:
    feats = features(panel)
    elig = eligibility(panel, feats)
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    outs = {h: outcomes(panel, h) for h in horizons}
    counts = elig.sum(axis=1)
    in_window = (panel.dates >= start) & (panel.dates <= end)
    per_h_dates: dict[int, list[int]] = {}
    for h in horizons:
        _, _, exit_idx = outs[h]
        ok = in_window & (exit_idx >= 0) & (counts >= spec.MIN_UNIVERSE)
        ok &= np.array([e >= 0 and panel.dates[e] <= end for e in exit_idx], dtype=bool)
        per_h_dates[h] = list(np.flatnonzero(ok))
    result = {h: [] for h in horizons}
    valid_sets = {h: set(ts) for h, ts in per_h_dates.items()}
    for t in sorted(set().union(*valid_sets.values())):
        el = np.flatnonzero(elig[t])
        n_names = len(el)
        size = decile_size(n_names)
        perms = np.stack(
            [np.random.default_rng([s, t]).permutation(n_names) for s in seeds]
        )
        long_idx, short_idx = perms[:, :size], perms[:, size : 2 * size]
        orders = {}
        for name in rankers:
            score = feats[name][t, el]
            orders[name] = (
                np.argsort(-score, kind="stable")[:size],
                np.argsort(score, kind="stable")[:size],
            )
        for h in horizons:
            if t not in valid_sets[h]:
                continue
            ret, bench, _ = outs[h]
            r = ret[t, el]
            row = {
                "t": t,
                "N": n_names,
                "n": size,
                "ew": _known_mean(r),
                "bench": bench[t],
                "unknown": float(np.isnan(r).mean()),
                "rand_long": _known_mean(r[long_idx]),
                "rand_short": _known_mean(r[short_idx]),
                "long": {k: _known_mean(r[o[0]]) for k, o in orders.items()},
                "short": {k: _known_mean(r[o[1]]) for k, o in orders.items()},
            }
            result[h].append(row)
    series = {}
    for h, rows in result.items():
        series[h] = HorizonSeries(
            dates=pd.DatetimeIndex([panel.dates[r["t"]] for r in rows]),
            universe=np.array([r["N"] for r in rows]),
            size=np.array([r["n"] for r in rows]),
            ew=np.array([r["ew"] for r in rows], dtype=float),
            bench=np.array([r["bench"] for r in rows], dtype=float),
            unknown_share=np.array([r["unknown"] for r in rows], dtype=float),
            rand_long=np.array([r["rand_long"] for r in rows], dtype=float).reshape(
                len(rows), len(seeds)
            ),
            rand_short=np.array([r["rand_short"] for r in rows], dtype=float).reshape(
                len(rows), len(seeds)
            ),
            long={
                k: np.array([r["long"][k] for r in rows], dtype=float) for k in rankers
            },
            short={
                k: np.array([r["short"][k] for r in rows], dtype=float) for k in rankers
            },
        )
    return series


def newey_west_t(x: np.ndarray, lags: int) -> float | None:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < max(30, 2 * lags):
        return None
    d = x - x.mean()
    gamma0 = float(d @ d) / n
    var = gamma0
    for lag in range(1, lags + 1):
        weight = 1 - lag / (lags + 1)
        var += 2 * weight * float(d[lag:] @ d[:-lag]) / n
    if var <= 0:
        return None
    return float(x.mean() / math.sqrt(var / n))


def _nanmean(a: np.ndarray, axis=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(a, axis=axis)


def cell_metrics(cell: spec.Cell, hs: HorizonSeries) -> dict:
    """Statistics of one grid cell; every number is net of the matched per-side cost."""
    cost = spec.COST_BPS[cell.market] / 1e4
    sides = spec.SIDES_PAID[cell.side]
    long, short = hs.long[cell.ranker], hs.short[cell.ranker]
    if cell.side == "long_only":
        x = long - hs.ew
        rand_x = hs.rand_long - hs.ew[:, None]
        absolute = long - sides * cost
    else:
        x = long - short - sides * cost
        rand_x = hs.rand_long - hs.rand_short - sides * cost
        absolute = x
    mean_x = _nanmean(x)
    seed_means = _nanmean(np.where(np.isfinite(x)[:, None], rand_x, np.nan), axis=0)
    random_p = float((1 + np.sum(seed_means >= mean_x)) / (1 + len(seed_means)))
    years = {}
    for year in spec.YEARS:
        mask = np.asarray(hs.dates.year == int(year))
        years[year] = float(_nanmean(x[mask])) if mask.sum() >= 20 else None
    known_years = [v for v in years.values() if v is not None]
    metrics = {
        "dates": int(np.isfinite(x).sum()),
        "mean_universe": float(hs.universe.mean()) if len(hs.universe) else None,
        "mean_decile_size": float(hs.size.mean()) if len(hs.size) else None,
        "unknown_outcome_share": float(hs.unknown_share.mean())
        if len(hs.unknown_share)
        else None,
        "net_primary_mean": float(mean_x),
        "net_absolute_mean": float(_nanmean(absolute)),
        "long_gross_mean": float(_nanmean(long)),
        "short_gross_mean": float(_nanmean(short))
        if cell.side == "long_short"
        else None,
        "ew_universe_mean": float(_nanmean(hs.ew)),
        "benchmark_mean": float(_nanmean(hs.bench)),
        "long_excess_vs_ew": float(_nanmean(long - hs.ew)),
        "long_excess_vs_benchmark": float(_nanmean(long - hs.bench)),
        "short_leg_excess_vs_ew": float(_nanmean(hs.ew - short))
        if cell.side == "long_short"
        else None,
        "short_leg_excess_vs_benchmark": float(_nanmean(hs.bench - short))
        if cell.side == "long_short"
        else None,
        "random_mean": float(_nanmean(seed_means)),
        "random_p05": float(np.nanpercentile(seed_means, 5)),
        "random_p50": float(np.nanpercentile(seed_means, 50)),
        "random_p95": float(np.nanpercentile(seed_means, 95)),
        "random_p": random_p,
        "t_newey_west": newey_west_t(x, cell.horizon),
        "year_means": years,
        "positive_years": int(sum(v > 0 for v in known_years)),
        "years_measured": len(known_years),
    }
    return metrics


def carry_forward(rows: list[dict]) -> list[str]:
    """Pre-registered selection of cells for the one validation look."""
    passing = []
    for row in rows:
        m = row["metrics"]
        if m["t_newey_west"] is None or m["dates"] < 100:
            continue
        ok = (
            m["net_primary_mean"] > 0
            and (row["side"] == "long_short" or m["long_excess_vs_benchmark"] > 0)
            and m["t_newey_west"] >= spec.CARRY_MIN_T
            and m["random_p"] <= spec.CARRY_MAX_RANDOM_P
            and m["positive_years"] >= spec.CARRY_MIN_POSITIVE_YEARS
        )
        if ok:
            passing.append((-m["t_newey_west"], row["cell"]))
    return [cell for _, cell in sorted(passing)[: spec.CARRY_CAP]]
