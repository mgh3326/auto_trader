"""B2 -- classify and bound the D+20 censoring. Informative, not just truncation.

The adversarial review showed that D+20 unscoreable samples are not only
corpus-end truncation: some symbols stop producing bars while the market keeps
trading, those samples are disproportionately B-only, and the ones that still
have a D+5 are badly negative. Dropping them silently biases the surviving
D+20 distribution upward, and it biases it more for B-A than for A.

This module does three things and asserts nothing beyond them:

1. splits unscoreable D+20 samples into ``corpus_end`` and ``terminal_gap``
2. reports the terminal_gap cohort mix and their observable D+5 outcomes
3. computes a bounded sensitivity: re-scores D+20 under explicit worst / D+5
   carry / best assumptions for the censored rows, so the reader sees the
   range the missing outcomes could occupy instead of a single number that
   quietly excludes them

🔴 It does not impute a value and then report it as measured. Every bound is
labelled by the assumption that produced it.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from statistics import median
from typing import Any

COHORTS = ("a_and_b", "b_only", "neither")
FULL_WINDOW = 20


def classify(sample: dict[str, Any]) -> str:
    """corpus_end | terminal_gap | scoreable."""
    if sample["scores"][str(FULL_WINDOW)]["scoreable"]:
        return "scoreable"
    if int(sample.get("global_forward_sessions", 0)) < FULL_WINDOW:
        # The market itself had not run 20 more sessions by the corpus cutoff.
        return "corpus_end"
    # The market kept trading; this symbol did not.
    return "terminal_gap"


def _returns(rows: list[dict[str, Any]], window: str) -> list[float]:
    out = []
    for row in rows:
        score = row["scores"][window]
        if score["scoreable"]:
            out.append(float(score["primary"]["simple_return_to_close"]))
    return out


def _stat(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median": round(median(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "win_rate": round(sum(1 for v in values if v > 0) / len(values), 6),
    }


def analyse(result: dict[str, Any]) -> dict[str, Any]:
    samples = result["samples"]
    by_cohort: dict[str, dict[str, Any]] = {}

    for cohort in COHORTS:
        rows = [row for row in samples if row["cohort"] == cohort]
        buckets = Counter(classify(row) for row in rows)
        terminal = [row for row in rows if classify(row) == "terminal_gap"]
        terminal_d5 = _returns(terminal, "5")
        scoreable_d20 = _returns(rows, "20")

        # Bounded sensitivity for D+20. The censored rows have no D+20 outcome;
        # each bound states the assumption it used for them.
        worst_pool = list(scoreable_d20)
        carry_pool = list(scoreable_d20)
        best_pool = list(scoreable_d20)
        observed_min = min(scoreable_d20) if scoreable_d20 else -1.0
        observed_max = max(scoreable_d20) if scoreable_d20 else 1.0
        for row in terminal:
            d5 = row["scores"]["5"]
            worst_pool.append(-1.0)  # total loss
            best_pool.append(observed_max)
            if d5["scoreable"]:
                carry_pool.append(float(d5["primary"]["simple_return_to_close"]))
            else:
                carry_pool.append(observed_min)

        by_cohort[cohort] = {
            "n_total": len(rows),
            "buckets": dict(buckets),
            "terminal_gap_rate": (
                round(buckets["terminal_gap"] / len(rows), 6) if rows else None
            ),
            "corpus_end_rate": (
                round(buckets["corpus_end"] / len(rows), 6) if rows else None
            ),
            "terminal_gap_observable_d5": _stat(terminal_d5),
            "d20_as_reported_excludes_censored": _stat(scoreable_d20),
            "d20_bounded_sensitivity": {
                "assumption_worst_total_loss": _stat(worst_pool),
                "assumption_carry_d5_else_observed_min": _stat(carry_pool),
                "assumption_best_observed_max": _stat(best_pool),
            },
        }

    a_rate = by_cohort["a_and_b"]["terminal_gap_rate"] or 0.0
    b_rate = by_cohort["b_only"]["terminal_gap_rate"] or 0.0
    return {
        "market": result["market"],
        "window": FULL_WINDOW,
        "cohorts": by_cohort,
        "asymmetry": {
            "terminal_gap_rate_a_and_b": a_rate,
            "terminal_gap_rate_b_only": b_rate,
            "b_only_over_a_ratio": (round(b_rate / a_rate, 4) if a_rate else None),
            "direction": (
                "b_only loses more rows to informative censoring than a_and_b"
                if b_rate > a_rate
                else "a_and_b loses more rows to informative censoring than b_only"
                if a_rate > b_rate
                else "equal"
            ),
            "consequence": (
                "the reported D+20 distribution is biased upward for whichever "
                "cohort loses more censored rows, so a D+20 tail-risk or "
                "full-distribution equivalence claim is not supported"
            ),
        },
        "primary_window_note": (
            "D+5 is the common window: it is far less censored, so the D+5 "
            "comparison carries the primary result and D+20 is read only "
            "alongside these bounds"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json")
    args = parser.parse_args()
    with open(args.result_json, encoding="utf-8") as handle:
        result = json.load(handle)
    print(json.dumps(analyse(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
