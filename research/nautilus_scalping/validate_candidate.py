#!/usr/bin/env python3
"""ROB-320 — validated-signal gate driver.

For each target symbol, backtests: the candidate over a small param grid, the
micro-breakout baseline, and a seeded random-entry control. Trades are merged
across symbols (chronologically) and fed to ``validated_gate.evaluate_gate``.
Writes a ``validated_signal_gate.v1`` JSON report.

NO execution side effects: public-data backtest only. Nothing here submits,
schedules, mutates a broker/DB, reads secrets, or applies params to a daemon.

Usage (research venv):
    PYTHONPATH=../.. .venv/bin/python validate_candidate.py --catalog catalog \
        --symbols XRPUSDT,BTCUSDT --candidate meanrev_zscore_fade \
        --window-from 2026-03-01 --window-to 2026-05-14 \
        --export results/rob320/meanrev.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest_runner import run
from candidates import get_candidate
from validated_gate import (
    Trade,
    apply_statistical_evidence,
    bootstrap_sharpe_ci,
    evaluate_gate,
    monte_carlo_permutation,
    net_pnls_at_fee,
    write_run_card,
)

# small, fixed param grid (param-stability check, not optimization)
_GRID = {
    "meanrev_zscore_fade": [
        ("z2.0/tp30/sl30", {"lookback": 20, "z_entry": "2.0", "tp_bps": 30, "sl_bps": 30}),
        ("z2.5/tp40/sl40", {"lookback": 20, "z_entry": "2.5", "tp_bps": 40, "sl_bps": 40}),
    ],
}


def _merge(runs: list[list[Trade]]) -> list[Trade]:
    return sorted((t for r in runs for t in r), key=lambda t: t.ts_opened)


def main() -> int:
    ap = argparse.ArgumentParser(description="ROB-320 validated-signal gate driver")
    ap.add_argument("--catalog", type=Path, default="catalog")
    ap.add_argument("--symbols", default="XRPUSDT,BTCUSDT")
    ap.add_argument("--candidate", default="meanrev_zscore_fade")
    ap.add_argument("--trade-size", default="100")
    ap.add_argument("--fee-bps", type=float, default=10.0)
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    ap.add_argument("--export", type=Path, default="results/rob320/gate.json")
    ap.add_argument("--seed", type=int, default=42,
                    help="seed for bootstrap / Monte-Carlo permutation")
    ap.add_argument("--bootstrap-n", type=int, default=1000)
    ap.add_argument("--mc-n", type=int, default=1000)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cand = get_candidate(args.candidate)
    grid = _GRID[args.candidate]

    print(f"Loading catalog from: {args.catalog.resolve()}")
    print(f"Target symbols     : {symbols}")
    print(f"Running candidate  : {args.candidate}")
    print("Param stability grid:")
    for label, params in grid:
        print(f"  - {label}: {params}")

    # candidate: per param label, merge trades across all symbols
    candidate_runs: dict[str, list[Trade]] = {}
    for label, params in grid:
        print(f"Running backtests for candidate config {label}...")
        per_symbol = []
        for sym in symbols:
            size = "0.002" if sym == "BTCUSDT" else args.trade_size
            per_symbol.append(run(args.catalog, sym, args.candidate, params, size,
                                   args.window_from, args.window_to))
        candidate_runs[label] = _merge(per_symbol)
        print(f"  -> Total trades across all symbols: {len(candidate_runs[label])}")

    # baselines (merged across symbols)
    print("Running backtests for baseline trend micro-breakout...")
    breakout_runs = []
    for sym in symbols:
        size = "0.002" if sym == "BTCUSDT" else args.trade_size
        breakout_runs.append(run(args.catalog, sym, "micro_breakout",
                                 get_candidate("micro_breakout").default_params, size,
                                 args.window_from, args.window_to))
    breakout = _merge(breakout_runs)
    print(f"  -> Total trades: {len(breakout)}")

    print("Running backtests for baseline seeded random control...")
    random_runs = []
    for sym in symbols:
        size = "0.002" if sym == "BTCUSDT" else args.trade_size
        random_runs.append(run(args.catalog, sym, "random_entry",
                               get_candidate("random_entry").default_params, size,
                               args.window_from, args.window_to))
    random_ctrl = _merge(random_runs)
    print(f"  -> Total trades: {len(random_ctrl)}")

    print("Evaluating walk-forward gate report...")
    report = evaluate_gate(
        candidate_runs=candidate_runs, baseline_breakout=breakout, baseline_random=random_ctrl,
        fee_bps=args.fee_bps, min_trades=args.min_trades,
        candidate_name=cand.name, hypothesis=cand.hypothesis, symbols=symbols,
        window={"from": args.window_from, "to": args.window_to,
                "folds": {"train": 0.5, "val": 0.25, "oos": 0.25}},
    )

    # ROB-328 (ROB-327 F1) — statistical robustness of the net-after-fee result.
    # Bootstrap CI + Monte-Carlo permutation on the val-best config's per-trade
    # net PnLs. This tests whether the (known net-negative; ROB-316/320) result
    # is statistically robust, and emits an audit run card. No new edge claim.
    val_best = report.param_stability.get("val_best_param")
    bootstrap = monte_carlo = None
    if val_best in candidate_runs:
        net_pnls = net_pnls_at_fee(candidate_runs[val_best], args.fee_bps)
        bootstrap = bootstrap_sharpe_ci(net_pnls, n_bootstrap=args.bootstrap_n, seed=args.seed)
        monte_carlo = monte_carlo_permutation(net_pnls, n_sim=args.mc_n, seed=args.seed)
        apply_statistical_evidence(report, bootstrap)  # folds CI evidence into verdict

    args.export.parent.mkdir(parents=True, exist_ok=True)
    args.export.write_text(json.dumps(report.to_dict(), indent=2))

    run_card_config = {
        "candidate": args.candidate, "symbols": symbols, "fee_bps": args.fee_bps,
        "min_trades": args.min_trades, "grid": dict(grid),
        "window": {"from": args.window_from, "to": args.window_to},
        "seed": args.seed, "bootstrap_n": args.bootstrap_n, "mc_n": args.mc_n,
    }
    card_paths = write_run_card(
        report, args.export.parent,
        config=run_card_config,
        data_sources=[f"binance_demo_backtest:{','.join(symbols)}"],
        bootstrap=bootstrap, monte_carlo=monte_carlo,
    )

    print("\n=============================================================")
    print(f"VERDICT: {report.verdict.upper()}")
    print("Reasons:")
    for reason in report.verdict_reasons:
        print(f"  - {reason}")
    print("=============================================================")
    print(f"Total trades of val-best config: {report.trade_count}")
    print(f"Report exported to: {args.export.resolve()}")
    print(f"Run card: {card_paths['json'].resolve()} / {card_paths['md'].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
