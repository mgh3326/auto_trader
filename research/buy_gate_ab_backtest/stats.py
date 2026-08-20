"""Precision annex: cluster bootstrap over decision dates.

🔴 Added AFTER the addendum freeze. It is disclosed as such in the report.
It cannot select an outcome: the identical resampling is applied to all three
cohorts and both windows, and it changes no per-sample number — it only puts
an interval around aggregates ``run_backtest`` already produced.

Clustering is by decision date, not by sample. Samples overlap heavily (a
20-session window spans four consecutive decision dates, and a whole
cross-section shares one date's market move), so an i.i.d. bootstrap would
report an interval several times too narrow.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from statistics import median
from typing import Any

import numpy as np

BOOTSTRAP_DRAWS = 2000
SEED = 20260821


def _by_date(samples: list[dict[str, Any]], cohort: str, window: str) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in samples:
        if row["cohort"] != cohort:
            continue
        score = row["scores"][window]
        if not score["scoreable"]:
            continue
        grouped[row["decision_date"]].append(
            float(score["primary"]["simple_return_to_close"])
        )
    return grouped


def _cluster_bootstrap(
    grouped: dict[str, list[float]], rng: np.random.Generator
) -> dict[str, Any]:
    dates = list(grouped)
    if len(dates) < 2:
        return {"n_clusters": len(dates), "median_ci95": None, "mean_ci95": None}
    medians: list[float] = []
    means: list[float] = []
    index = np.arange(len(dates))
    for _ in range(BOOTSTRAP_DRAWS):
        picked = rng.choice(index, size=len(dates), replace=True)
        pooled: list[float] = []
        for position in picked:
            pooled.extend(grouped[dates[position]])
        if not pooled:
            continue
        medians.append(median(pooled))
        means.append(sum(pooled) / len(pooled))
    if not medians:
        return {"n_clusters": len(dates), "median_ci95": None, "mean_ci95": None}
    return {
        "n_clusters": len(dates),
        "median_ci95": [
            round(float(np.percentile(medians, 2.5)), 6),
            round(float(np.percentile(medians, 97.5)), 6),
        ],
        "mean_ci95": [
            round(float(np.percentile(means, 2.5)), 6),
            round(float(np.percentile(means, 97.5)), 6),
        ],
    }


def _paired_difference(
    left: dict[str, list[float]],
    right: dict[str, list[float]],
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Bootstrap the median gap between two cohorts on the *same* dates."""
    shared = sorted(set(left) & set(right))
    if len(shared) < 2:
        return {"n_shared_dates": len(shared), "median_diff_ci95": None}
    diffs: list[float] = []
    index = np.arange(len(shared))
    for _ in range(BOOTSTRAP_DRAWS):
        picked = rng.choice(index, size=len(shared), replace=True)
        pooled_left: list[float] = []
        pooled_right: list[float] = []
        for position in picked:
            pooled_left.extend(left[shared[position]])
            pooled_right.extend(right[shared[position]])
        if not pooled_left or not pooled_right:
            continue
        diffs.append(median(pooled_left) - median(pooled_right))
    if not diffs:
        return {"n_shared_dates": len(shared), "median_diff_ci95": None}
    return {
        "n_shared_dates": len(shared),
        "median_diff_point": round(
            median([value for values in left.values() for value in values])
            - median([value for values in right.values() for value in values]),
            6,
        ),
        "median_diff_ci95": [
            round(float(np.percentile(diffs, 2.5)), 6),
            round(float(np.percentile(diffs, 97.5)), 6),
        ],
        "share_of_draws_favouring_left": round(
            sum(1 for value in diffs if value > 0) / len(diffs), 4
        ),
    }


def annex(result: dict[str, Any]) -> dict[str, Any]:
    rng = np.random.default_rng(SEED)
    samples = result["samples"]
    out: dict[str, Any] = {
        "market": result["market"],
        "method": "cluster_bootstrap_over_decision_dates",
        "draws": BOOTSTRAP_DRAWS,
        "seed": SEED,
        "added_after_addendum_freeze": True,
        "windows": {},
    }
    for window in ("5", "20"):
        grouped = {
            cohort: _by_date(samples, cohort, window)
            for cohort in ("a_and_b", "b_only", "neither")
        }
        out["windows"][window] = {
            "cohorts": {
                cohort: _cluster_bootstrap(grouped[cohort], rng)
                for cohort in grouped
            },
            "b_only_minus_a_and_b": _paired_difference(
                grouped["b_only"], grouped["a_and_b"], rng
            ),
            "b_only_minus_neither": _paired_difference(
                grouped["b_only"], grouped["neither"], rng
            ),
            "a_and_b_minus_neither": _paired_difference(
                grouped["a_and_b"], grouped["neither"], rng
            ),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json")
    args = parser.parse_args()
    with open(args.result_json, encoding="utf-8") as handle:
        result = json.load(handle)
    print(json.dumps(annex(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
