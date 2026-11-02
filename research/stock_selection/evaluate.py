"""Run all baselines and the fixed ranker on identical walk-forward dates."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .dataset import FEATURE_CONTRACT, HORIZONS, build_dataset, sessions
from .io import add_input_args, read_bars, write_json
from .metrics import score_daily
from .models import BASELINES, LGBM_CONFIG, ranker_predict
from .splits import walk_forward


def evaluate(
    dataset: pd.DataFrame,
    *,
    market: str,
    first_test: str,
    train_start: str,
    test_end: str,
    top_n: int = 5,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict:
    if not horizons or any(h not in HORIZONS for h in horizons):
        raise ValueError("supported horizons required")
    calendar = sessions(market, train_start, test_end)
    folds = walk_forward(calendar, first_test=first_test)
    eligible = dataset[dataset.eligible]
    predictions = {h: [] for h in horizons}
    for fold in folds:
        train = eligible[eligible.date.isin(fold.train_dates)]
        test = eligible[eligible.date.isin(fold.test_dates)]
        if test.empty:
            raise ValueError("empty evaluation fold")
        for h in horizons:
            if (train[f"label_end_{h}"] >= fold.test_dates[0]).any():
                raise ValueError("training label crosses evaluation boundary")
            scored = test.copy()
            scored["lightgbm_rank"] = ranker_predict(train, test, h)
            predictions[h].append(scored)
    results = {}
    for h, chunks in predictions.items():
        panel = pd.concat(chunks, ignore_index=True)
        results[str(h)] = {}
        for name in [*BASELINES, "lightgbm_rank"]:
            scored = panel.assign(score=panel[name] * BASELINES.get(name, 1.0))
            results[str(h)][name] = {
                "raw": score_daily(scored, market=market, horizon=h, top_n=top_n),
                "relative": score_daily(
                    scored, market=market, horizon=h, top_n=top_n, relative=True
                ),
            }
    return {
        "market": market,
        "feature_contract": FEATURE_CONTRACT,
        "results": results,
        "eligibility_audit": {
            "expected_validation_sessions": sum(len(f.test_dates) for f in folds),
            "scored_sessions": int(panel.date.nunique()),
            "missing_validation_sessions": [
                str(d.date())
                for d in pd.DatetimeIndex(
                    [d for f in folds for d in f.test_dates]
                ).difference(pd.DatetimeIndex(panel.date.unique()))
            ],
            "min_names": int(panel.groupby("date").size().min()),
            "max_names": int(panel.groupby("date").size().max()),
        },
        "folds": [
            {
                "train": [str(f.train_dates[0].date()), str(f.train_dates[-1].date())],
                "test": [str(f.test_dates[0].date()), str(f.test_dates[-1].date())],
            }
            for f in folds
        ],
        "ranker_config": LGBM_CONFIG,
        "top_n": top_n,
    }


def render_tables(payload: dict) -> str:
    lines = [
        f"# {payload['market']} — SURVIVOR-ONLY / BOUNDED diagnostic walk-forward",
        "",
        f"Folds: `{payload['folds']}`",
        "",
    ]

    def number(value):
        return "UNKNOWN" if value is None else f"{value:.6f}"

    for h, models in payload["results"].items():
        lines += [
            f"## h={h} — SURVIVOR-ONLY / BOUNDED",
            "",
            "| Model | IC | ICIR | Spread | Up hit | Down hit | Turnover | Net spread | IC/spread days | Coverage |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, targets in models.items():
            s = targets["raw"]["summary"]
            lines.append(
                "| "
                + " | ".join(
                    [name]
                    + [
                        number(s[key])
                        for key in (
                            "ic",
                            "icir",
                            "decile_spread",
                            "up_hit",
                            "down_hit",
                            "turnover",
                            "cost_adjusted_spread",
                        )
                    ]
                    + [
                        f"{s['ic_days']}/{s['complete_spread_days']}",
                        number(s["outcome_coverage"]),
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_args(parser)
    parser.add_argument("--train-start", default="2021-01-01")
    parser.add_argument("--first-test", default="2022-01-01")
    parser.add_argument("--test-end", default="2022-12-31")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument(
        "--horizon",
        type=int,
        choices=HORIZONS,
        action="append",
        help="split a bounded run by horizon; default all four",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    bars, meta = read_bars(args.cache_dir, args.market)
    dataset = build_dataset(bars, args.market, liquid_n=args.liquid_n)
    payload = evaluate(
        dataset,
        market=args.market,
        first_test=args.first_test,
        train_start=args.train_start,
        test_end=args.test_end,
        top_n=args.top_n,
        horizons=tuple(args.horizon) if args.horizon else HORIZONS,
    )
    payload["input"] = meta
    suffix = "-h" + "-".join(map(str, args.horizon)) if args.horizon else ""
    path = write_json(
        args.output_dir / f"{args.market}-evaluation{suffix}.json", payload
    )
    table = path.with_suffix(".md")
    table.write_text(render_tables(payload))
    print(path)
    print(table)


if __name__ == "__main__":
    main()
