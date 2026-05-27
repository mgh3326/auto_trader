#!/usr/bin/env python3
"""ROB-324 — maker/limit-fill edge re-evaluation driver.

Produces three scenarios and feeds each to the UNCHANGED validated_gate:
  1. Taker baseline   — ROB-320 taker trades, gate's native rescale to 4.0 bps
  2. Maker optimistic — data-derived limit fills at real maker/taker fees
  3. Maker conservative — (2) minus queue-loss drop + adverse-selection cost  [HEADLINE]

NO execution side effects: public-data backtest only. Nothing submits, schedules,
mutates a broker/DB, reads secrets, or applies params to a daemon.

Usage (rob-320 venv):
    export CATALOG=/Users/mgh3326/work/auto_trader.rob-320/research/nautilus_scalping/catalog
    PYTHONPATH=../.. $NVENV validate_maker_fill.py --catalog "$CATALOG" \
        --symbols XRPUSDT,BTCUSDT --window-from 2026-03-01 --window-to 2026-05-14 \
        --export results/rob324/maker_fill.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest_runner import run, run_maker
from candidates import get_candidate
from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    build_maker_conservative,
    build_maker_optimistic,
)
from validated_gate import (
    REF_FEE_BPS,
    Trade,
    apply_statistical_evidence,
    bootstrap_sharpe_ci,
    evaluate_gate,
    monte_carlo_permutation,
    net_pnls_at_fee,
)

_GRID = [
    ("z2.0/tp30/sl30", {"lookback": 20, "z_entry": "2.0", "tp_bps": 30, "sl_bps": 30}),
    ("z2.5/tp40/sl40", {"lookback": 20, "z_entry": "2.5", "tp_bps": 40, "sl_bps": 40}),
]
_SIZE = {"BTCUSDT": "0.002"}
_FILL_MODEL = {
    "entry_rule": "passive limit @ signal-bar close",
    "fill_timeout_bars": 1,
    "tp_execution": "maker limit",
    "sl_execution": "taker stop (market)",
    "queue_loss_pct": 0.25,
    "adverse_bps": 1.0,
    "excursion_eps_bps": 2.0,
}


def _merge(runs: list[list[Trade]]) -> list[Trade]:
    return sorted((t for r in runs for t in r), key=lambda t: t.ts_opened)


def _merge_recs(runs):
    return sorted((rec for r in runs for rec in r), key=lambda rec: rec.ts_opened)


def _gate(candidate_runs, breakout, random_ctrl, fee_bps, symbols, window, name, seed=42):
    report = evaluate_gate(
        candidate_runs=candidate_runs,
        baseline_breakout=breakout, baseline_random=random_ctrl,
        fee_bps=fee_bps, min_trades=100,
        candidate_name=name, hypothesis="mean_reversion",
        symbols=symbols, window=window,
    )
    # ROB-328 statistical robustness on the val-best config's net-after-fee per-trade PnL:
    # bootstrap Sharpe CI + Monte-Carlo permutation. apply_statistical_evidence only
    # downgrades a validated verdict (CI upper < 0); it never upgrades. No new edge claim.
    val_best = report.param_stability.get("val_best_param")
    stats: dict = {}
    if val_best and val_best in candidate_runs:
        net_pnls = net_pnls_at_fee(candidate_runs[val_best], fee_bps)
        bootstrap = bootstrap_sharpe_ci(net_pnls, n_bootstrap=1000, seed=seed)
        monte_carlo = monte_carlo_permutation(net_pnls, n_sim=1000, seed=seed)
        apply_statistical_evidence(report, bootstrap)
        stats = {"bootstrap_sharpe_ci": bootstrap, "monte_carlo_permutation": monte_carlo}
    out = report.to_dict()
    out.update(stats)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="ROB-324 maker/limit-fill driver")
    ap.add_argument("--catalog", type=Path, default="catalog")
    ap.add_argument("--symbols", default="XRPUSDT,BTCUSDT")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--export", type=Path, default="results/rob324/maker_fill.json")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    window = {"from": args.window_from, "to": args.window_to,
              "folds": {"train": 0.5, "val": 0.25, "oos": 0.25}}

    # --- baselines (taker, single config each), merged across symbols ---
    print("Running taker baselines (breakout, random)...")
    breakout = _merge([run(args.catalog, s, "micro_breakout",
                           get_candidate("micro_breakout").default_params,
                           _SIZE.get(s, "100")) for s in symbols])
    random_ctrl = _merge([run(args.catalog, s, "random_entry",
                              get_candidate("random_entry").default_params,
                              _SIZE.get(s, "100")) for s in symbols])

    # --- scenario 1: taker baseline candidate over the param grid (grid rescales 10->4) ---
    print("Scenario 1: taker baseline @ 4 bps (grid)...")
    taker_runs = {label: _merge([run(args.catalog, s, "meanrev_zscore_fade",
                                     dict(params), _SIZE.get(s, "100")) for s in symbols])
                  for label, params in _GRID}
    taker_report = _gate(taker_runs, breakout, random_ctrl, TAKER_BASELINE_BPS,
                         symbols, window, "meanrev_taker_baseline")

    # --- scenarios 2 & 3: maker re-sim over the SAME grid (param-stability preserved) ---
    print("Scenarios 2 & 3: maker re-sim (grid)...")
    maker_recs: dict[str, list] = {}
    attempted = filled = 0
    for label, params in _GRID:
        per_symbol = []
        for s in symbols:
            recs, att, fil = run_maker(args.catalog, s, dict(params), _SIZE.get(s, "100"))
            per_symbol.append(recs)
            attempted += att
            filled += fil
        maker_recs[label] = _merge_recs(per_symbol)

    opt_runs = {label: build_maker_optimistic(recs) for label, recs in maker_recs.items()}
    con_runs = {label: build_maker_conservative(
                    recs, queue_loss_pct=_FILL_MODEL["queue_loss_pct"],
                    adverse_bps=_FILL_MODEL["adverse_bps"],
                    excursion_eps_bps=_FILL_MODEL["excursion_eps_bps"])
                for label, recs in maker_recs.items()}

    # maker scenarios: net already at real fees -> evaluate at REF (as-run)
    opt_report = _gate(opt_runs, breakout, random_ctrl, REF_FEE_BPS,
                       symbols, window, "meanrev_maker_optimistic")
    con_report = _gate(con_runs, breakout, random_ctrl, REF_FEE_BPS,
                       symbols, window, "meanrev_maker_conservative")

    artifact = {
        "schema_version": "validated_signal_gate.v2",
        "candidate": "meanrev_zscore_fade",
        "hypothesis": "mean_reversion",
        "symbols": symbols,
        "window": window,
        "cost_model": {
            "maker_fee_bps": MAKER_FEE_BPS, "taker_fee_bps": TAKER_BASELINE_BPS,
            "commission_source": "results/rob324/binance_usdm_commission_rates.json",
            "note": ("maker scenarios bake real per-leg fees into net; gate evaluated "
                     "at its reference point (as-run). taker baseline uses the gate's "
                     "native single-rate rescale to 4.0 bps."),
        },
        "fill_model": _FILL_MODEL,
        "fill_stats": {"entries_attempted": attempted, "entries_filled": filled,
                       "missed_fills": attempted - filled},
        "scenarios": {
            "taker_baseline": taker_report,
            "maker_optimistic": opt_report,
            "maker_conservative": con_report,
        },
        "verdict": con_report["verdict"],            # headline = conservative (honest bound)
        "verdict_reasons": con_report["verdict_reasons"],
        "verdict_source": "maker_conservative",
    }

    args.export.parent.mkdir(parents=True, exist_ok=True)
    args.export.write_text(json.dumps(artifact, indent=2))
    print("\n=============================================================")
    print(f"HEADLINE VERDICT (conservative): {artifact['verdict'].upper()}")
    for r in artifact["verdict_reasons"]:
        print(f"  - {r}")
    print(f"taker_baseline   verdict: {taker_report['verdict']}")
    print(f"maker_optimistic verdict: {opt_report['verdict']}")
    print(f"missed fills: {attempted - filled} / {attempted} attempted")
    print(f"Report exported to: {args.export.resolve()}")
    print("=============================================================")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
