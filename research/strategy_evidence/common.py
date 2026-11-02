"""Shared constants, sample statistics and the pre-registered retire rule."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: ``order_proposals.auto_approve.round_trip_cost_bps`` in config/trading_policy.yaml.
#: These are the policy's conservative maximum, not measured fees: the export has no
#: commission or tax column.
ROUND_TRIP_COST = {"kr": 0.00474, "us": 0.0090, "crypto": 0.0010}
#: ``cash_yields`` in config/trading_policy.yaml, annual, pre-tax, operator-reported.
#: KIS cash has no entry there, so it is taken as 0%.
IDLE_CASH_YIELD = {
    ("toss_live", "kr"): 0.010,
    ("toss_live", "us"): 0.006,
    ("upbit_live", "crypto"): 0.021,
}
#: The underwater-d20-v1 and held_majors retire rule: below this many mature filled
#: observations the review is deferred.
MIN_SAMPLE = 5
RULE_MEDIAN_MIN = 0.0
RULE_Q1_MIN_EXCLUSIVE = -0.08
INSUFFICIENT = "INSUFFICIENT_SAMPLE"
MARKETS = ("kr", "us", "crypto")
#: Thresholds are inclusive ("exactly -8.00% qualifies"), and 92 / 100 - 1 is
#: -0.07999... in floating point, so comparisons allow this much slack.
TOLERANCE = 1e-9
MARKET_NAMES = {"kr": "KR", "us": "US", "crypto": "Crypto"}


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float
    median: float
    q1: float
    q3: float
    minimum: float
    positive: float

    @property
    def sufficient(self) -> bool:
        return self.n >= MIN_SAMPLE


def summarize(values) -> Summary:
    """Sample statistics. Quartiles are numpy's linear interpolation."""
    arr = np.asarray([v for v in values if v is not None and v == v], dtype=float)
    if not len(arr):
        nan = float("nan")
        return Summary(0, nan, nan, nan, nan, nan, nan)
    return Summary(
        n=len(arr),
        mean=float(arr.mean()),
        median=float(np.median(arr)),
        q1=float(np.percentile(arr, 25)),
        q3=float(np.percentile(arr, 75)),
        minimum=float(arr.min()),
        positive=float((arr > 0).mean()),
    )


def retire_verdict(summary: Summary) -> str:
    """The pre-registered rule: kept only if median >= 0 and lower quartile > -8%."""
    if not summary.sufficient:
        return INSUFFICIENT
    passed = summary.median >= RULE_MEDIAN_MIN and summary.q1 > RULE_Q1_MIN_EXCLUSIVE
    return "PASS (keep)" if passed else "FAIL (retire)"
