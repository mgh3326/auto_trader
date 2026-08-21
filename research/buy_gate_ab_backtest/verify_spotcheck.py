"""Independent spot-check of a result file, straight from the parquet corpus.

    uv run python -m research.buy_gate_ab_backtest.verify_spotcheck RESULT.json

Deliberately does NOT import the runner, the scorer, or the loader: it reopens
the corpus itself and recomputes entry price and the D+5 / D+20 close returns
by hand. If the pipeline and this disagree, one of them is wrong and the run is
not reportable.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys

import pandas as pd

_KR_ROOT = (
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/runs/"
    "kr-corpus-v1-20260803-1001/dataset"
)
_US_ROOT = "/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/dataset"
_CRYPTO_ROOT = "/Users/mgh3326/work/herdr-artifacts/crypto-corpus-v1/dataset"


def _kr_bars(symbol: str) -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(_KR_ROOT, "**", f"ticker={symbol}.parquet"), recursive=True))
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    frame["d"] = pd.to_datetime(frame["session"])
    return frame


def _us_bars(symbol: str) -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(_US_ROOT, "**", "*.parquet"), recursive=True))
    parts = []
    for path in paths:
        frame = pd.read_parquet(path)
        parts.append(frame[frame["symbol"] == symbol])
    frame = pd.concat(parts, ignore_index=True)
    frame["d"] = pd.to_datetime(frame["session_date"])
    return frame


def _crypto_bars(symbol: str, venue: str) -> pd.DataFrame:
    root = os.path.join(_CRYPTO_ROOT, f"venue={venue}")
    paths = [
        path
        for path in glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True)
        if os.path.basename(path).startswith(f"{symbol}__1d__")
    ]
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    frame = frame[frame["frequency"] == "1d"]
    frame["d"] = (
        pd.to_datetime(frame["open_time_utc"], utc=True).dt.tz_localize(None).dt.normalize()
    )
    return frame


def bars_for(market: str, symbol: str) -> pd.DataFrame:
    if market == "kr":
        frame = _kr_bars(symbol)
    elif market == "us":
        frame = _us_bars(symbol)
    else:
        frame = _crypto_bars(symbol, market.removeprefix("crypto_"))
    frame = frame[frame["close"] > 0]
    return (
        frame.drop_duplicates(subset=["d"], keep="last")
        .sort_values("d")
        .reset_index(drop=True)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json")
    parser.add_argument("--n", type=int, default=8)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    with open(args.result_json, encoding="utf-8") as handle:
        result = json.load(handle)
    market = result["market"]
    scoreable = [row for row in result["samples"] if row["scores"]["20"]["scoreable"]]
    if not scoreable:
        print("no scoreable sample to check")
        return 1
    random.seed(args.seed)
    picks = random.sample(scoreable, min(args.n, len(scoreable)))

    cache: dict[str, pd.DataFrame] = {}
    failures = 0
    for row in picks:
        symbol = row["symbol"]
        if symbol not in cache:
            cache[symbol] = bars_for(market, symbol)
        frame = cache[symbol]
        decision = pd.Timestamp(row["decision_date"])
        entry = float(row["entry_price"])
        corpus_close = float(frame.loc[frame["d"] == decision, "close"].iloc[0])
        forward = frame[frame["d"] > decision].reset_index(drop=True)
        # The live get_support_resistance_impl rounds current_price to 2
        # decimals, so the frozen entry is the cent-rounded close, not the raw
        # one. That rounding is reproduced deliberately, not worked around --
        # but anything beyond half a cent would be a real defect.
        if abs(entry - round(corpus_close, 2)) > 1e-9:
            print(
                f"FAIL entry {symbol} {decision.date()}: "
                f"{entry} != round({corpus_close}, 2)"
            )
            failures += 1
        elif abs(entry - corpus_close) > 1e-6:
            print(
                f"note {symbol} {decision.date()}: entry {entry} is the "
                f"cent-rounded close {corpus_close} "
                f"({abs(entry - corpus_close) / corpus_close * 100:.4f}%)"
            )
        for window in ("5", "20"):
            expected = float(row["scores"][window]["primary"]["simple_return_to_close"])
            actual = (float(forward["close"].iloc[int(window) - 1]) - entry) / entry
            status = "OK" if abs(expected - actual) < 1e-9 else "MISMATCH"
            if status != "OK":
                failures += 1
            print(
                f"{symbol:12s} {row['decision_date']} D+{window:>2s} "
                f"pipeline={expected:+.6f} independent={actual:+.6f} {status}"
            )
    print(f"\n{'ALL MATCH' if failures == 0 else f'{failures} MISMATCH'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
