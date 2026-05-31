"""Run the bounded 1m gross-edge spike and emit a counts-only artifact.

Bounded by construction: ONE month, ONE parameter set, a fixed symbol list. No
parameter sweep (that is an explicit non-goal). The artifact carries only
counts/metrics — never raw bars.

Usage (standalone, no repo deps):
    python3 run.py                          # default: 2026-04, XRPUSDT + BTCUSDT
    python3 run.py --year 2026 --month 4 --out

Symbols: XRPUSDT is the Binance USD-M Futures Demo default (executable). BTCUSDT
is included as a liquid DATA reference only — it is explicitly excluded from the
demo execution allowlist (MIN_NOTIONAL=50 > 10 USDT cap; see
``app/services/brokers/binance/futures_demo/sizing.py``), so its result does NOT
map to anything the demo loop could trade.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from binance_1m_data import fetch_month_closes
from fees import (
    ECONOMIC_TRIVIALITY_FLOOR_BPS,
    MAKER_BPS_PER_LEG,
    TAKER_BPS_PER_LEG,
)
from meanrev_probe import ProbeParams, probe_symbol, result_to_dict

_SCHEMA = "binance_1m_gross_edge_spike.v1"
_RESULTS = Path(__file__).resolve().parent / "results"

# (symbol, demo_executable)
_SYMBOLS = [("XRPUSDT", True), ("BTCUSDT", False)]


def _parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=4)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--z-entry", type=float, default=2.0, dest="z_entry")
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument(
        "--out", action="store_true", help="also write results/<schema>.json"
    )
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    params = ProbeParams(lookback=args.lookback, z_entry=args.z_entry, hold=args.hold)

    symbols_out = []
    for symbol, demo_exec in _SYMBOLS:
        _times, closes = fetch_month_closes(symbol, args.year, args.month)
        res = probe_symbol(symbol, closes, demo_executable=demo_exec, params=params)
        symbols_out.append(result_to_dict(res))

    any_gross = any(
        s["verdict"] == "gross_edge_present_needs_full_validation" for s in symbols_out
    )
    all_screened = symbols_out and all(
        s["verdict"] == "screened_out_gross" for s in symbols_out
    )
    overall = (
        "gross_edge_candidate_found"
        if any_gross
        else ("screened_out_gross_all" if all_screened else "needs_more_data")
    )

    artifact = {
        "schema_version": _SCHEMA,
        "window": {"year": args.year, "month": args.month, "interval": "1m"},
        "market": "binance_usdm_futures",
        "params": {
            "lookback": params.lookback,
            "z_entry": params.z_entry,
            "hold": params.hold,
            "signal": "zscore_meanrev_fade_nonoverlapping",
        },
        "fees_bps_per_leg": {
            "taker": TAKER_BPS_PER_LEG,
            "maker": MAKER_BPS_PER_LEG,
            "economic_triviality_floor_gross": ECONOMIC_TRIVIALITY_FLOOR_BPS,
        },
        "symbols": symbols_out,
        "overall_verdict": overall,
        "caveats": [
            "Tiny single-month sample; NOT a profitability claim and NOT a validation.",
            "GROSS edge only; net columns subtract a flat round-trip fee for context.",
            "Single parameter set; no sweep (explicit non-goal).",
            "BTCUSDT is data-only (excluded from futures-demo execution).",
        ],
    }

    print(json.dumps(artifact, indent=2))
    if args.out:
        _RESULTS.mkdir(parents=True, exist_ok=True)
        path = _RESULTS / f"{_SCHEMA}.json"
        path.write_text(json.dumps(artifact, indent=2))
        print(f"\nartifact written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
