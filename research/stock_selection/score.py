"""Grade an immutable past daily list after each h-session outcome matures."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd

from .dataset import (
    FEATURE_CONTRACT,
    HORIZONS,
    LABEL_KIND,
    build_dataset,
    decision_at,
    sessions,
)
from .forward import prefix_digest
from .io import add_input_args, digest, read_bars, write_json
from .metrics import score_daily


def grade_lists(bars: pd.DataFrame, lists: dict, *, asof: str, market: str) -> dict:
    if lists.get("feature_contract") != FEATURE_CONTRACT:
        raise ValueError(
            "forward feature contract differs; use the recorded harness version"
        )
    if lists["market"] != market or lists["schema"] != "stock-selection-forward/v1":
        raise ValueError("forward market/schema mismatch")
    # Earlier v1 records already use the same statistical close-return contract.
    # New metadata cannot relabel them as an executable trade outcome.
    prediction_at = decision_at(market, pd.Timestamp(lists["asof"]).date()).isoformat()
    if (
        lists.get("label_kind", LABEL_KIND) != LABEL_KIND
        or lists.get("execution_label") is not None
    ):
        raise ValueError("execution labels require a separate scorer")
    if (
        lists.get("prediction_at", prediction_at) != prediction_at
        or lists.get("data_cutoff", lists["asof"]) != lists["asof"]
    ):
        raise ValueError("forward prediction time/data cutoff mismatch")
    if asof < lists["asof"]:
        raise ValueError("grading date precedes list")
    if prefix_digest(bars, lists["asof"]) != lists["input_prefix_sha256"]:
        raise ValueError("historical input changed since the list was recorded")
    prefix = bars[pd.to_datetime(bars.date) <= pd.Timestamp(asof)]
    dataset = build_dataset(prefix, market, liquid_n=lists["liquid_n"])
    origin = dataset[dataset.date == lists["asof"]]
    start = pd.Timestamp(lists["asof"])
    future_sessions = sessions(
        market, lists["asof"], (start + timedelta(days=60)).date().isoformat()
    )
    grades = {}
    for h in HORIZONS:
        maturity = future_sessions[h].date().isoformat()
        if maturity > asof:
            grades[str(h)] = {"state": "pending", "matures_on": maturity}
            continue
        saved = lists["horizons"][str(h)]
        scores = pd.DataFrame(saved["scores"])
        if scores.duplicated("symbol").any() or set(scores.symbol) != set(
            origin[origin.eligible].symbol
        ):
            raise ValueError("forward scores do not match eligible cross-section")
        ordered = scores.sort_values(
            ["score", "symbol"], ascending=[False, True]
        ).to_dict("records")
        n = lists["top_n"]
        if saved["up"] != ordered[:n] or saved["down"] != list(reversed(ordered[-n:])):
            raise ValueError("saved top-N lists disagree with scores")
        scored = origin.merge(scores, on="symbol", how="inner", validate="one_to_one")
        raw = score_daily(scored, market=market, horizon=h, top_n=n)
        grades[str(h)] = {
            "state": "scored"
            if raw["summary"]["outcome_coverage"] == 1
            else "unknown_outcome",
            "matures_on": maturity,
            "raw": raw,
            "relative": score_daily(
                scored, market=market, horizon=h, top_n=n, relative=True
            ),
        }
    return {
        "schema": "stock-selection-grade/v1",
        "label_kind": LABEL_KIND,
        "prediction_at": prediction_at,
        "data_cutoff": lists["asof"],
        "execution_label": None,
        "market": market,
        "list_asof": lists["asof"],
        "graded_asof": asof,
        "horizons": grades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_args(parser)
    parser.add_argument(
        "--asof", required=True, help="last completed bar date available for grading"
    )
    parser.add_argument("--lists", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    bars, _ = read_bars(args.cache_dir, args.market)
    lists = json.loads(args.lists.read_text())
    payload = grade_lists(bars, lists, asof=args.asof, market=args.market)
    payload["list_sha256"] = digest(args.lists)
    path = (
        (args.output_dir or args.cache_dir / "grades")
        / args.market
        / f"{lists['asof']}-{lists['model']}-graded-{args.asof}.json"
    )
    print(write_json(path, payload))


if __name__ == "__main__":
    main()
