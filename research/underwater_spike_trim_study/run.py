"""CLI: scan one market's frozen corpus and persist every observation.

    uv run python -m research.underwater_spike_trim_study.run --market crypto

Writes ``observations.jsonl`` plus a ``scan-summary.json`` under the output
directory.  Aggregation lives in ``report.py`` so a re-cut of the tables never
re-reads the corpora.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .corpora import assert_offline_environment, load_market
from .events import scan_symbol
from .spec import LEVEL_WINDOW

DEFAULT_OUT = Path("/Users/mgh3326/work/herdr-artifacts/uwtrim-backtest-20260821")
MARKETS = ("crypto", "kr", "us")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, choices=MARKETS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="smoke-test cap; a capped run is labelled partial in the summary",
    )
    parser.add_argument(
        "--level-window",
        type=int,
        default=LEVEL_WINDOW,
        help=(
            "trailing sessions for the S/R proxy; 120 is the pre-registered primary, "
            "60 matches what the production get_support_resistance tool actually fetches"
        ),
    )
    parser.add_argument(
        "--price-decimals",
        type=int,
        default=None,
        help="round level prices like the production tool does (2); default is full precision",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    assert_offline_environment()

    suffix = "" if args.level_window == LEVEL_WINDOW else f"-w{args.level_window}"
    out_dir = args.out / f"{args.market}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    observations_path = out_dir / "observations.jsonl"

    started = time.monotonic()
    totals = {
        "symbols_loaded": 0,
        "symbols_with_events": 0,
        "bars_scanned": 0,
        "eligible_bars": 0,
        "prefilter_hits": 0,
        "events": 0,
        "events_dropped_degenerate_window": 0,
        "events_rejected_resistance": 0,
        "controls": 0,
        "dropped_invalid_rows": 0,
        "inconsistent_ohlc_rows": 0,
    }
    first_session: str | None = None
    last_session: str | None = None
    segments: dict[str, int] = {}

    with observations_path.open("w", encoding="utf-8") as handle:
        for bars in load_market(args.market, args.symbols):
            if (
                args.max_symbols is not None
                and totals["symbols_loaded"] >= args.max_symbols
            ):
                break
            totals["symbols_loaded"] += 1
            totals["dropped_invalid_rows"] += bars.dropped_invalid_rows
            totals["inconsistent_ohlc_rows"] += bars.inconsistent_ohlc_rows
            segments[bars.segment] = segments.get(bars.segment, 0) + 1
            if not bars.frame.empty:
                low = str(bars.frame["session"].iloc[0].date())
                high = str(bars.frame["session"].iloc[-1].date())
                first_session = (
                    low if first_session is None else min(first_session, low)
                )
                last_session = high if last_session is None else max(last_session, high)

            result = scan_symbol(
                bars,
                price_decimals=args.price_decimals,
                level_window=args.level_window,
            )
            totals["bars_scanned"] += result.bars_scanned
            totals["eligible_bars"] += result.eligible_bars
            totals["prefilter_hits"] += result.prefilter_hits
            totals["events"] += result.events
            totals["events_dropped_degenerate_window"] += (
                result.events_dropped_degenerate_window
            )
            totals["events_rejected_resistance"] += result.events_rejected_resistance
            totals["controls"] += sum(
                1 for o in result.observations if o.kind == "control"
            )
            if result.events:
                totals["symbols_with_events"] += 1
            for observation in result.observations:
                handle.write(json.dumps(asdict(observation), ensure_ascii=False) + "\n")

            if totals["symbols_loaded"] % 200 == 0:
                elapsed = time.monotonic() - started
                print(
                    f"  {args.market}: {totals['symbols_loaded']} symbols, "
                    f"{totals['events']} events, {elapsed:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )

    summary = {
        "market": args.market,
        "level_window": args.level_window,
        "price_decimals": args.price_decimals,
        "partial_run": args.max_symbols is not None or args.symbols is not None,
        "first_session": first_session,
        "last_session": last_session,
        "segments": segments,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        **totals,
    }
    (out_dir / "scan-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
