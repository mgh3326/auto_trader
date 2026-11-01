"""Gate verdict, fixed before the first fit.

Inputs are daily NAV paths of the 2019-2022 walk-forward run under the
conservative fill end. ``gbm-baseline`` passes only if all of these hold:

* G1 net vs cash: annualised net vs cash exceeds ``delta_min``;
* G2 every control: annualised daily-return difference exceeds ``delta_min``
  with a Holm-adjusted circular-block-bootstrap p <= ``alpha`` across the family;
* G3 stability: it beats cash and the unchanged policy in at least 3 of 4 years;
* G4 bracket: net vs cash is also positive under the optimistic end.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

import numpy as np

from research.kr_boosting.config import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    GATE_ALPHA,
    GATE_DELTA_MIN_ANNUAL,
    GATE_MIN_POSITIVE_YEARS,
    SESSIONS_PER_YEAR,
)

CONTINUE = "CONTINUE_TO_U8"
STOP = "STOP"


def daily_returns(nav: np.ndarray, capital: float) -> np.ndarray:
    """Equal-capital daily P&L as a share of starting capital."""

    full = np.concatenate([[capital], nav])
    return np.diff(full) / capital


def annualised(returns: np.ndarray) -> float:
    return float(returns.mean() * SESSIONS_PER_YEAR) if len(returns) else 0.0


def bootstrap_p(
    diff: np.ndarray,
    *,
    delta_min: float = GATE_DELTA_MIN_ANNUAL,
    block: int = BOOTSTRAP_BLOCK,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> float:
    """Share of circular-block resamples whose annualised mean is <= ``delta_min``."""

    n = len(diff)
    if n == 0:
        return 1.0
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(n / block))
    wrapped = np.concatenate([diff, diff[: block - 1]])
    prefix = np.concatenate([[0.0], np.cumsum(wrapped)])
    starts = rng.integers(0, n, size=(resamples, blocks))
    totals = (prefix[starts + block] - prefix[starts]).sum(axis=1)
    means = totals / (blocks * block)
    return float((means * SESSIONS_PER_YEAR <= delta_min).mean())


def holm(pvalues: Mapping[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values."""

    ordered = sorted(pvalues.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(ordered)
    out: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * p))
        out[name] = running
    return out


@dataclass(frozen=True, slots=True)
class Comparison:
    control: str
    annual_diff: float
    p_raw: float
    p_holm: float
    passed: bool


@dataclass(frozen=True, slots=True)
class Verdict:
    hypothesis_id: str
    verdict: str
    net_vs_cash_annual_conservative: float
    net_vs_cash_annual_optimistic: float
    g1_net_vs_cash: bool
    g2_controls: bool
    g3_stability: bool
    g4_bracket: bool
    positive_years_vs_cash: int
    positive_years_vs_unchanged: int
    comparisons: tuple[Comparison, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def decide(
    hypothesis_id: str,
    *,
    capital: float,
    gbm_conservative: np.ndarray,
    gbm_optimistic: np.ndarray,
    controls_conservative: Mapping[str, np.ndarray],
    unchanged: str,
    years: Sequence[int],
) -> Verdict:
    """All NAV arrays share ``years``' session axis; ``years[i]`` labels session i."""

    year_axis = np.asarray(years)
    gbm = daily_returns(gbm_conservative, capital)
    opt = daily_returns(gbm_optimistic, capital)
    cons_annual = annualised(gbm)
    opt_annual = annualised(opt)

    diffs = {
        name: gbm - daily_returns(nav, capital)
        for name, nav in controls_conservative.items()
    }
    raw = {name: bootstrap_p(diff) for name, diff in diffs.items()}
    adjusted = holm(raw)
    comparisons = tuple(
        Comparison(
            control=name,
            annual_diff=annualised(diffs[name]),
            p_raw=raw[name],
            p_holm=adjusted[name],
            passed=annualised(diffs[name]) > GATE_DELTA_MIN_ANNUAL
            and adjusted[name] <= GATE_ALPHA,
        )
        for name in sorted(diffs)
    )

    uniq = sorted(set(years))
    vs_cash = sum(float(gbm[year_axis == y].sum()) > 0 for y in uniq)
    vs_unchanged = sum(float(diffs[unchanged][year_axis == y].sum()) > 0 for y in uniq)

    g1 = cons_annual > GATE_DELTA_MIN_ANNUAL
    g2 = all(c.passed for c in comparisons)
    g3 = min(vs_cash, vs_unchanged) >= GATE_MIN_POSITIVE_YEARS
    g4 = opt_annual > 0
    return Verdict(
        hypothesis_id=hypothesis_id,
        verdict=CONTINUE if g1 and g2 and g3 and g4 else STOP,
        net_vs_cash_annual_conservative=cons_annual,
        net_vs_cash_annual_optimistic=opt_annual,
        g1_net_vs_cash=g1,
        g2_controls=g2,
        g3_stability=g3,
        g4_bracket=g4,
        positive_years_vs_cash=vs_cash,
        positive_years_vs_unchanged=vs_unchanged,
        comparisons=comparisons,
    )


def max_drawdown(nav: np.ndarray) -> float:
    if not len(nav):
        return 0.0
    peak = np.maximum.accumulate(nav)
    return float((nav / peak - 1.0).min())
