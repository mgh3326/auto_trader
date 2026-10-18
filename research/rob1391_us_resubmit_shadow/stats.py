"""Paired parent-level statistics (prereg §3 통계와 판정 규칙).

* estimand: ratio of sums, ``ΔM = Σ(x_T - x_C) / Σ B_i`` over parents;
* symbol cluster bootstrap, 10,000 replicates, seed 1383 (date-block
  bootstrap over D0 sessions as a sensitivity);
* null-centred one-sided p for ``ΔM > 0``:
  ``p = (1 + #{Δ*_b - Δ̂ >= Δ̂}) / (B + 1)``;
* Holm over the two p values: smallest ``<= 0.025``, next ``<= 0.05``; once
  one fails, every later one fails;
* percentile 95 % CI, denominator and effective cluster count reported.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from research.rob1391_us_resubmit_shadow import constants as K


@dataclass(frozen=True, slots=True)
class PairedRow:
    cluster: str
    budget: float
    diff: float  # x_T - x_C, same units as budget


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    estimate: float
    ci_low: float
    ci_high: float
    p_one_sided: float
    n_rows: int
    n_clusters: int
    denominator: float


def ratio(rows: Sequence[PairedRow]) -> float:
    denom = sum(r.budget for r in rows)
    return sum(r.diff for r in rows) / denom if denom else float("nan")


def null_centred_p(boot: np.ndarray, estimate: float) -> float:
    exceed = int(np.count_nonzero(boot - estimate >= estimate))
    return (1 + exceed) / (len(boot) + 1)


def cluster_bootstrap(
    rows: Sequence[PairedRow],
    *,
    replicates: int = K.BOOTSTRAP_REPLICATES,
    seed: int = K.SEED,
    rng: np.random.Generator | None = None,
) -> BootstrapResult:
    if not rows:
        nan = float("nan")
        return BootstrapResult(nan, nan, nan, nan, 0, 0, 0.0)
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        sums[r.cluster][0] += r.diff
        sums[r.cluster][1] += r.budget
    clusters = sorted(sums)
    diff = np.array([sums[c][0] for c in clusters])
    budget = np.array([sums[c][1] for c in clusters])
    gen = rng if rng is not None else np.random.default_rng(seed)
    idx = gen.integers(0, len(clusters), size=(replicates, len(clusters)))
    num = diff[idx].sum(axis=1)
    den = budget[idx].sum(axis=1)
    boot = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
    estimate = float(diff.sum() / budget.sum())
    alpha = 1 - K.CI_LEVEL
    low, high = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    return BootstrapResult(
        estimate=estimate,
        ci_low=float(low),
        ci_high=float(high),
        p_one_sided=null_centred_p(boot, estimate),
        n_rows=len(rows),
        n_clusters=len(clusters),
        denominator=float(budget.sum()),
    )


def paired_bootstrap(
    metrics: dict[str, Sequence[PairedRow]],
    *,
    replicates: int = K.BOOTSTRAP_REPLICATES,
    seed: int = K.SEED,
) -> dict[str, BootstrapResult]:
    """Same resampled clusters for every metric (one generator, fixed order)."""

    out: dict[str, BootstrapResult] = {}
    for name in sorted(metrics):
        out[name] = cluster_bootstrap(
            metrics[name], replicates=replicates, rng=np.random.default_rng(seed)
        )
    return out


def holm(
    pvalues: dict[str, float], alphas: Sequence[float] = K.HOLM_ALPHAS
) -> dict[str, bool]:
    """Holm step-down with the pre-registered thresholds, in ascending p order.

    Ties are broken by name so the result is deterministic.
    """

    if len(pvalues) != len(alphas):
        raise ValueError("Holm needs exactly one threshold per hypothesis")
    # NaN (no estimate) sorts last as the weakest result and never passes.
    order = sorted(
        pvalues,
        key=lambda name: (
            pvalues[name] if pvalues[name] == pvalues[name] else float("inf"),
            name,
        ),
    )
    passed: dict[str, bool] = {}
    still = True
    for name, alpha in zip(order, alphas, strict=True):
        p = pvalues[name]
        still = still and (p == p) and p <= alpha  # NaN never passes
        passed[name] = still
    return passed
