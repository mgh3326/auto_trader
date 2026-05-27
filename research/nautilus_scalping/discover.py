#!/usr/bin/env python3
"""ROB-339 — fast scalping-strategy discovery driver (pure pandas; no Nautilus).

Reads the catalog trade-tick parquet for each symbol over a real `[from, to)`
window, engineers features, screens the five hypothesis families, and writes a
non-canonical `scalping_discovery.v1` artifact (screened_out / needs_more_data /
promote_to_full_validation). NO execution side effects: public-data read only;
nothing submits, schedules, mutates a broker/DB, reads secrets, or applies params.

Usage (research venv or the repo uv venv):
    python discover.py --catalog <catalog> --symbols XRPUSDT,BTCUSDT \
        --window-from 2026-03-01 --window-to 2026-05-14 --fee-budget-bps 8
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from artifact_paths import resolve_artifact_path
from discovery.screen import build_artifact, classify


def discover_from_bars(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    fee_budget_bps: float,
    min_samples: int = 200,
    missed_fill_max: float = 0.6,
    entry_offset_bps: float = 5.0,
    oos_frac: float = 0.25,
    window: dict | None = None,
) -> dict:
    """Pure orchestration: bars -> features -> hypotheses -> classify -> artifact."""
    # imported here so the pure data/feature path is the only pandas entry point
    from discovery.features import add_features
    from discovery.hypotheses import run_all_hypotheses

    classified = []
    for symbol, bars in bars_by_symbol.items():
        featured = add_features(bars)
        for summary in run_all_hypotheses(
            featured,
            fee_budget_bps=fee_budget_bps,
            entry_offset_bps=entry_offset_bps,
            oos_frac=oos_frac,
            symbol=symbol,
        ):
            classified.append(
                classify(
                    summary, min_samples=min_samples, missed_fill_max=missed_fill_max
                )
            )

    run = {
        "symbols": list(bars_by_symbol),
        "window": window or {},
        "fee_budget_bps": fee_budget_bps,
        "min_samples": min_samples,
        "missed_fill_max": missed_fill_max,
        "entry_offset_bps": entry_offset_bps,
        "oos_frac": oos_frac,
    }
    return build_artifact(classified, run)


def write_discovery_artifact(
    artifact: dict, *, export: str | Path | None = None, run_id: str = "latest"
) -> Path:
    """Write the artifact JSON to ``export`` or ``<root>/discovery/<run_id>/discovery.json``."""
    out = (
        Path(export)
        if export
        else resolve_artifact_path("discovery", run_id, "discovery.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2))
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="ROB-339 scalping discovery / fast-screen")
    ap.add_argument("--catalog", type=Path, default="catalog")
    ap.add_argument("--symbols", default="XRPUSDT,BTCUSDT")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument(
        "--fee-budget-bps",
        type=float,
        default=8.0,
        help="round-trip fee+slippage budget (taker 4/4 = 8 bps default)",
    )
    ap.add_argument("--min-samples", type=int, default=200)
    ap.add_argument("--missed-fill-max", type=float, default=0.6)
    ap.add_argument("--entry-offset-bps", type=float, default=5.0)
    ap.add_argument("--oos-frac", type=float, default=0.25)
    ap.add_argument("--run-id", default="latest")
    ap.add_argument(
        "--export",
        type=Path,
        default=None,
        help="explicit artifact path; default uses the artifact root",
    )
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    # imported lazily: only the catalog-backed run needs the parquet loader
    from discovery.data import load_bars

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bars_by_symbol = {
        sym: load_bars(args.catalog, sym, args.window_from, args.window_to)
        for sym in symbols
    }
    artifact = discover_from_bars(
        bars_by_symbol,
        fee_budget_bps=args.fee_budget_bps,
        min_samples=args.min_samples,
        missed_fill_max=args.missed_fill_max,
        entry_offset_bps=args.entry_offset_bps,
        oos_frac=args.oos_frac,
        window={
            "from": args.window_from,
            "to": args.window_to,
            "oos_frac": args.oos_frac,
        },
    )
    out = write_discovery_artifact(artifact, export=args.export, run_id=args.run_id)

    print(f"hypotheses_tested: {artifact['hypotheses_tested']}")
    for h in artifact["hypotheses"]:
        print(f"  [{h['recommendation']:>27}] {h['symbol']}/{h['name']}: {h['reason']}")
    print(f"artifact: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
