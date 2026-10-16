"""Interval estimators used by every ROB-1383 table.

* proportions -> Wilson score interval (95%, z = 1.959964)
* means       -> percentile bootstrap over observations
* clustered   -> percentile bootstrap that resamples *symbols* (clusters) with
  replacement and keeps every observation of a drawn symbol together.

All bootstrap draws use a fixed seed so a rerun is byte-identical.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

Z95 = 1.959963984540054
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 1383


def wilson_ci(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for ``successes / n``.

    ``n == 0`` returns ``(nan, nan)`` -- an empty cell has no interval and must
    not be reported as ``[0, 0]``.
    """

    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"invalid counts successes={successes} n={n}")
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], float)
    if arr.size == 0:
        return (math.nan, math.nan)
    if arr.size == 1:
        return (float(arr[0]), float(arr[0]))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(resamples, arr.size))
    means = arr[idx].mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def cluster_bootstrap_ci(
    values: Sequence[float],
    clusters: Sequence[str],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Percentile CI of the pooled mean, resampling whole clusters.

    The statistic of each replicate is ``sum(values in drawn clusters) /
    count(values in drawn clusters)`` -- i.e. the observation-weighted mean, so
    for 0/1 values it is the pooled proportion, matching the point estimate.
    """

    if len(values) != len(clusters):
        raise ValueError("values and clusters must align")
    pairs = [
        (float(v), str(c))
        for v, c in zip(values, clusters, strict=True)
        if v is not None and np.isfinite(v)
    ]
    if not pairs:
        return (math.nan, math.nan)
    keys = sorted({c for _, c in pairs})
    sums = dict.fromkeys(keys, 0.0)
    counts = dict.fromkeys(keys, 0)
    for v, c in pairs:
        sums[c] += v
        counts[c] += 1
    s = np.array([sums[k] for k in keys])
    n = np.array([counts[k] for k in keys], float)
    if len(keys) == 1:
        point = float(s[0] / n[0])
        return (point, point)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(resamples, len(keys)))
    stats = s[idx].sum(axis=1) / n[idx].sum(axis=1)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def proportion_row(
    flags: Sequence[bool], clusters: Sequence[str] | None = None
) -> dict[str, float | int]:
    """Point estimate + Wilson CI (+ symbol-cluster CI when clusters given)."""

    n = len(flags)
    k = int(sum(bool(f) for f in flags))
    lo, hi = wilson_ci(k, n)
    row: dict[str, float | int] = {
        "n": n,
        "k": k,
        "rate": (k / n) if n else math.nan,
        "wilson_lo": lo,
        "wilson_hi": hi,
    }
    if clusters is not None:
        clo, chi = cluster_bootstrap_ci([float(bool(f)) for f in flags], clusters)
        row["cluster_lo"] = clo
        row["cluster_hi"] = chi
        row["n_clusters"] = len(set(clusters))
    return row


def mean_row(values: Sequence[float], prefix: str) -> dict[str, float | int]:
    arr = [float(v) for v in values if v is not None and np.isfinite(v)]
    lo, hi = bootstrap_mean_ci(arr)
    return {
        f"{prefix}_n": len(arr),
        f"{prefix}_mean": float(np.mean(arr)) if arr else math.nan,
        f"{prefix}_median": float(np.median(arr)) if arr else math.nan,
        f"{prefix}_lo": lo,
        f"{prefix}_hi": hi,
    }


def ratio_ci(
    num: Sequence[float],
    den: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Percentile bootstrap (resampling items) of ``sum(num) / sum(den)``."""

    n_arr = np.asarray(num, float)
    d_arr = np.asarray(den, float)
    if len(n_arr) < 2 or d_arr.sum() == 0:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(n_arr), size=(resamples, len(n_arr)))
    stats = n_arr[idx].sum(axis=1) / d_arr[idx].sum(axis=1)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))
