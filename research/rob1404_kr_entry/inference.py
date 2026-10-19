"""Dependence-preserving intervals and the minimum detectable effect.

Every interval resamples whole units so the pooled (observation-weighted)
mean keeps the dependence inside a unit:

* ``iid``     -- candidates (v1 ``bootstrap_mean_ci``; reference only).
* ``symbol``  -- symbols with all their candidates (same algorithm and seed
  as v1 ``cluster_bootstrap_ci``; a test pins the equality).
* ``parent``  -- parent orders (re-submission chains), order level only.
* ``date_block`` -- moving blocks of ``block`` consecutive KRX sessions over
  the *full* session calendar between the first and last ``D0`` (empty days
  stay in, so a block spans calendar time). All candidates whose ``D0`` falls
  on a drawn session come along, preserving same-day common shocks and part
  of the overlap of 5/20-session holding windows.

The headline interval is the **envelope** of the dependence-preserving ones
(lowest lower bound, highest upper bound over symbol · parent · date blocks of
5/10/20 sessions): a verdict must survive every dependence assumption, not
just the single widest one (round-2 review: a "< 0" call that held only for
the 10-session block). The minimum detectable effect (one-sided alpha 5%,
power 80%) is ``(z_0.95 + z_0.80) x SE`` with ``SE`` the largest replicate-
mean standard deviation among them; the design effect is ``SE^2 / SE_iid^2``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from research.rob1383_buy_drought.stats import DEFAULT_RESAMPLES, bootstrap_mean_ci

SEED = 1404
Z95_ONE_SIDED = 1.6448536269514722
Z80 = 0.8416212335729143
MDE_FACTOR = Z95_ONE_SIDED + Z80
DEFAULT_BLOCK = 10
ENVELOPE_BLOCKS = (5, 10, 20)


def _percentile(stats: np.ndarray) -> tuple[float, float]:
    stats = stats[np.isfinite(stats)]
    if stats.size == 0:
        return (math.nan, math.nan)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def cluster_replicates(
    values: Sequence[float],
    clusters: Sequence[str],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = SEED,
) -> np.ndarray:
    """Replicate pooled means, resampling clusters (v1 cluster bootstrap)."""

    keys = sorted(set(clusters))
    sums = dict.fromkeys(keys, 0.0)
    counts = dict.fromkeys(keys, 0)
    for v, c in zip(values, clusters, strict=True):
        sums[c] += float(v)
        counts[c] += 1
    s = np.array([sums[k] for k in keys])
    n = np.array([counts[k] for k in keys], float)
    if len(keys) < 2:
        return np.full(resamples, s.sum() / n.sum() if n.sum() else math.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(resamples, len(keys)))
    return s[idx].sum(axis=1) / n[idx].sum(axis=1)


def block_replicates(
    values: Sequence[float],
    days: Sequence[Any],
    calendar: Sequence[Any],
    *,
    block: int = DEFAULT_BLOCK,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = SEED,
) -> np.ndarray:
    """Moving-block bootstrap over the session calendar (pooled mean)."""

    cal = list(calendar)
    pos = {d: i for i, d in enumerate(cal)}
    if any(d not in pos for d in days):
        raise ValueError("every observation day must be on the calendar")
    t = len(cal)
    s = np.zeros(t)
    n = np.zeros(t)
    for v, d in zip(values, days, strict=True):
        s[pos[d]] += float(v)
        n[pos[d]] += 1
    b = max(1, min(block, t))
    starts_max = t - b + 1
    k = math.ceil(t / b)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, starts_max, size=(resamples, k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(resamples, -1)[
        :, :t
    ]
    num = s[idx].sum(axis=1)
    den = n[idx].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def summarise(
    values: Sequence[float],
    *,
    symbols: Sequence[str],
    days: Sequence[Any],
    calendar: Sequence[Any],
    parents: Sequence[str] | None = None,
    block: int = DEFAULT_BLOCK,
) -> dict[str, float | int | str]:
    v = np.asarray(values, float)
    if v.size == 0:
        return {"n": 0}
    # Block over the sessions this subset actually spans (first..last D0).
    calendar = [d for d in calendar if min(days) <= d <= max(days)]
    out: dict[str, float | int | str] = {
        "n": int(v.size),
        "n_symbols": len(set(symbols)),
        "n_days": len(set(days)),
        "mean": float(v.mean()),
        "median": float(np.median(v)),
        "sd": float(v.std(ddof=1)) if v.size > 1 else math.nan,
    }
    iid_lo, iid_hi = bootstrap_mean_ci(v.tolist(), seed=SEED)
    out.update({"iid_lo": iid_lo, "iid_hi": iid_hi})
    rng = np.random.default_rng(SEED)
    iid_reps = v[rng.integers(0, v.size, size=(DEFAULT_RESAMPLES, v.size))].mean(1)
    se_iid = float(np.std(iid_reps, ddof=1)) if v.size > 1 else math.nan
    reps = {
        "symbol": cluster_replicates(v, list(symbols)),
        "date_block": block_replicates(v, list(days), calendar, block=block),
    }
    for extra in ENVELOPE_BLOCKS:
        if extra != block:
            reps[f"date_block{extra}"] = block_replicates(
                v, list(days), calendar, block=extra
            )
    if parents is not None:
        reps["parent"] = cluster_replicates(v, list(parents))
    los, his, ses, members = [], [], [], []
    for name, r in reps.items():
        lo, hi = _percentile(r)
        out[f"{name}_lo"], out[f"{name}_hi"] = lo, hi
        se = float(np.nanstd(r, ddof=1))
        out[f"{name}_se"] = se
        if math.isfinite(lo) and math.isfinite(hi):
            los.append(lo)
            his.append(hi)
            ses.append(se)
            members.append(name)
    if members:
        out["headline_ci"] = "envelope(" + ",".join(members) + ")"
        out["ci_lo"], out["ci_hi"] = min(los), max(his)
        out["ci_lo_from"] = members[los.index(min(los))]
        out["ci_hi_from"] = members[his.index(max(his))]
        se = max(ses)
        out["mde_one_sided_80"] = MDE_FACTOR * se
        out["design_effect"] = (
            se**2 / se_iid**2 if se_iid and math.isfinite(se_iid) else math.nan
        )
    return out


def verdict(lo: float, hi: float) -> str:
    """Wording rule: a CI spanning 0 is '판별 불가', never 'no edge'."""

    if not (math.isfinite(lo) and math.isfinite(hi)):
        return "판별 불가(구간 없음)"
    if lo == 0.0 and hi == 0.0:
        return "0(체결 없음 — 현금과 동일)"
    if hi < 0:
        return "음(구간 전체 < 0)"
    if lo > 0:
        return "양(구간 전체 > 0)"
    return "판별 불가(구간이 0 을 포함)"


def required_n(sd: float, delta: float, design_effect: float) -> float:
    """Candidates needed to detect ``delta`` (one-sided 5%, power 80%)."""

    if not (sd > 0 and delta > 0):
        return math.nan
    deff = design_effect if math.isfinite(design_effect) and design_effect > 0 else 1.0
    return (MDE_FACTOR**2) * (sd / delta) ** 2 * deff
