"""Write dated up/down research lists using only bars through --asof."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from .dataset import FEATURE_CONTRACT, HORIZONS, build_dataset
from .io import add_input_args, read_bars, write_json
from .models import BASELINES, ranker_predict


def prefix_digest(bars: pd.DataFrame, asof: str) -> str:
    prefix = bars[pd.to_datetime(bars.date) <= pd.Timestamp(asof)].sort_values(
        ["date", "symbol"]
    )
    return hashlib.sha256(prefix.to_csv(index=False).encode()).hexdigest()


def make_lists(
    bars: pd.DataFrame,
    *,
    market: str,
    asof: str,
    model: str = "momentum_20",
    top_n: int = 5,
    liquid_n: int | None = None,
) -> dict:
    if model not in {*BASELINES, "lightgbm_rank"} or top_n < 1:
        raise ValueError("unsupported model or top_n")
    prefix = bars[pd.to_datetime(bars.date) <= pd.Timestamp(asof)]
    dataset = build_dataset(prefix, market, liquid_n=liquid_n)
    today = dataset[dataset.eligible & (dataset.date == pd.Timestamp(asof))].copy()
    if len(today) < 2 * top_n:
        raise ValueError(
            "insufficient eligible names for disjoint top-N lists on --asof"
        )
    days = pd.DatetimeIndex(sorted(dataset.date.unique()))
    train_days = days[:-20]  # max horizon purge 10 + embargo 10, before decision
    train = dataset[dataset.eligible & dataset.date.isin(train_days)]
    horizons = {}
    for h in HORIZONS:
        today["score"] = (
            ranker_predict(train, today, h)
            if model == "lightgbm_rank"
            else today[model] * BASELINES[model]
        )
        ordered = today.sort_values(["score", "symbol"], ascending=[False, True])
        scores = [
            {"symbol": str(row.symbol), "score": float(row.score)}
            for row in ordered.itertuples()
        ]
        horizons[str(h)] = {
            "up": scores[:top_n],
            "down": list(reversed(scores[-top_n:])),
            "scores": scores,
        }
    return {
        "schema": "stock-selection-forward/v1",
        "feature_contract": FEATURE_CONTRACT,
        "market": market,
        "asof": asof,
        "model": model,
        "top_n": top_n,
        "liquid_n": liquid_n,
        "input_prefix_sha256": prefix_digest(bars, asof),
        "horizons": horizons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_args(parser)
    parser.add_argument("--asof", required=True)
    parser.add_argument(
        "--model", choices=[*BASELINES, "lightgbm_rank"], default="momentum_20"
    )
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    bars, meta = read_bars(args.cache_dir, args.market)
    payload = make_lists(
        bars,
        market=args.market,
        asof=args.asof,
        model=args.model,
        top_n=args.top_n,
        liquid_n=args.liquid_n,
    )
    payload["input_provenance"] = meta
    path = (
        (args.output_dir or args.cache_dir / "forward")
        / args.market
        / f"{args.asof}-{args.model}.json"
    )
    if path.exists():
        if json.loads(path.read_text()) != payload:
            raise FileExistsError(
                "dated forward list already exists with different content"
            )
    else:
        write_json(path, payload)
    print(path)


if __name__ == "__main__":
    main()
