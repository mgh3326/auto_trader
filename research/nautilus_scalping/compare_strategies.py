#!/usr/bin/env python3
"""ROB-316 — compare strategies net-after-cost on the same 14d XRPUSDT ticks.

Strategies:
  * breakout 30/20   — original micro-breakout baseline
  * breakout 100/100 — the only fee-sweep combo with gross edge
  * ict 100/100      — ICT-filtered breakout (session+vol+FVG)
  * ict 100/100 +sweep — adds the liquidity-sweep filter (ablation)

Method mirrors fee_sweep: one BacktestEngine per subprocess (Nautilus Rust
logger is a process-global singleton), gross trades captured at REF_FEE_BPS,
net recomputed exactly for each fee. Profit is judged ONLY after realistic fees;
the 0-fee column is gross-edge reference, reported separately.

Reports per (strategy, fee): trades, net PnL, win-rate, avg bps/trade, max
drawdown. Flags overfit risk when trade count is low.

Usage:
    python compare_strategies.py --catalog catalog --symbol XRPUSDT --trade-size 100
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import cost_model

REF_FEE_BPS = cost_model.REF_FEE_BPS
FEE_GRID_BPS = [10.0, 7.5, 5.0, 2.0, 0.0]
OVERFIT_MIN_TRADES = 30
_SENTINEL = "RESULT_JSON "

# (label, strategy, tp, sl, flags)  — flags toggle ICT filters to ablate which
# one collapses the trade count / adds edge. breakout ignores flags.
SPECS = [
    ("breakout 100/100", "breakout", 100, 100, {}),
    ("ict S+V+F (all)", "ict", 100, 100, {"session": True, "vol": True, "fvg": True}),
    ("ict S+V (no FVG)", "ict", 100, 100, {"session": True, "vol": True, "fvg": False}),
    ("ict V+F (no sess)", "ict", 100, 100, {"session": False, "vol": True, "fvg": True}),
    ("ict vol-only", "ict", 100, 100, {"session": False, "vol": True, "fvg": False}),
    ("ict session-only", "ict", 100, 100, {"session": True, "vol": False, "fvg": False}),
]


def _run_single(catalog_path, symbol, strategy, tp, sl, trade_size, flags):
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_path))
    instrument = next(i for i in catalog.instruments() if i.id.value.startswith(symbol))
    ticks = catalog.trade_ticks(instrument_ids=[instrument.id.value])

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="CMP-001", logging=LoggingConfig(log_level="ERROR")
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"), oms_type=OmsType.HEDGING, account_type=AccountType.CASH,
        base_currency=None, starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(instrument)
    engine.add_data(ticks)
    bar_type = BarType.from_str(f"{instrument.id.value}-1-MINUTE-LAST-INTERNAL")

    if strategy == "breakout":
        from strategy_breakout import BreakoutScalper, BreakoutScalperConfig
        strat = BreakoutScalper(BreakoutScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type,
            trade_size=trade_size, tp_bps=tp, sl_bps=sl))
    else:
        from strategy_ict import IctScalper, IctScalperConfig
        strat = IctScalper(IctScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type, trade_size=trade_size,
            tp_bps=tp, sl_bps=sl,
            require_session=flags.get("session", True),
            require_vol=flags.get("vol", True),
            require_fvg=flags.get("fvg", True),
            require_sweep=flags.get("sweep", False)))
    engine.add_strategy(strat)
    engine.run()

    trades = []
    for p in engine.cache.positions_closed():
        net_ref = p.realized_pnl.as_double()
        comm_ref = sum(c.as_double() for c in p.commissions())
        notional = float(p.avg_px_open) * float(p.peak_qty)
        trades.append([net_ref, comm_ref, notional, int(p.ts_opened)])
    engine.dispose()
    print(_SENTINEL + json.dumps({"trades": trades}))


def _metrics(trades, fee_bps):
    """Return (n, net_pnl, win_rate_pct, avg_bps, mdd) at a per-leg fee."""
    rows = sorted(trades, key=lambda t: t[3])  # chronological for MDD
    nets, bps = [], []
    for net_ref, comm_ref, notional, _ in rows:
        net = cost_model.net_at_fee(net_ref, comm_ref, fee_bps, REF_FEE_BPS)
        nets.append(net)
        bps.append(net / notional * 1e4 if notional else 0.0)
    n = len(nets)
    if n == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    wins = sum(1 for x in nets if x > 0)
    equity = peak = mdd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
    return n, sum(nets), 100 * wins / n, sum(bps) / n, mdd


def _worker(catalog, symbol, strategy, tp, sl, size, flags):
    cmd = [sys.executable, os.path.abspath(__file__), "--single",
           "--strategy", strategy, "--tp", str(tp), "--sl", str(sl),
           "--catalog", str(catalog), "--symbol", symbol, "--trade-size", size,
           "--flags", json.dumps(flags)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            return json.loads(line[len(_SENTINEL):])["trades"]
    raise RuntimeError(f"worker {strategy} {tp}/{sl} failed:\n{proc.stderr[-700:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare strategies net-after-cost")
    ap.add_argument("--catalog", default="catalog", type=Path)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--trade-size", default="100")
    ap.add_argument("--export", default="results/compare.csv", type=Path)
    ap.add_argument("--specs", default="",
                    help="comma-sep label substrings to include (default: all)")
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--strategy", choices=["breakout", "ict"])
    ap.add_argument("--tp", type=int)
    ap.add_argument("--sl", type=int)
    ap.add_argument("--flags", default="{}")
    args = ap.parse_args()

    if args.single:
        _run_single(args.catalog, args.symbol, args.strategy, args.tp, args.sl,
                    args.trade_size, json.loads(args.flags))
        return 0

    print(f"compare: {args.symbol}, size={args.trade_size}, fees={FEE_GRID_BPS}")
    print("profit judged at realistic fee (10/7.5 bps taker); 0bps = gross-edge reference only.\n")

    specs = SPECS if not args.specs else [
        s for s in SPECS if any(k.strip() in s[0] for k in args.specs.split(","))
    ]
    out_rows = []
    for label, strategy, tp, sl, flags in specs:
        trades = _worker(args.catalog, args.symbol, strategy, tp, sl, args.trade_size, flags)
        n_total = len(trades)
        flag = "  <-- LOW TRADES (overfit risk)" if n_total < OVERFIT_MIN_TRADES else ""
        print(f"### {label}   (trades={n_total}){flag}")
        print(f"   {'fee':>5} {'net_pnl':>10} {'win%':>6} {'avg_bps':>8} {'max_dd':>9}")
        for fee in FEE_GRID_BPS:
            n, net, win, avg_bps, mdd = _metrics(trades, fee)
            tag = "GROSS" if fee == 0.0 else f"{fee:.1f}"
            print(f"   {tag:>5} {net:>+10.1f} {win:>6.1f} {avg_bps:>+8.1f} {mdd:>9.1f}")
            out_rows.append({
                "strategy": label, "fee_bps_per_leg": fee, "trades": n,
                "net_pnl_usdt": round(net, 2), "win_rate_pct": round(win, 1),
                "avg_bps_per_trade": round(avg_bps, 2), "max_drawdown_usdt": round(mdd, 2),
                "low_trade_overfit_flag": n_total < OVERFIT_MIN_TRADES,
            })
        print()

    print("verdict (realistic taker fees):")
    for label, *_ in [(s[0],) for s in specs]:
        net10 = next(r["net_pnl_usdt"] for r in out_rows if r["strategy"] == label and r["fee_bps_per_leg"] == 10.0)
        net75 = next(r["net_pnl_usdt"] for r in out_rows if r["strategy"] == label and r["fee_bps_per_leg"] == 7.5)
        print(f"  {label:>22}: 10bps={net10:+.1f}  7.5bps={net75:+.1f}")

    args.export.parent.mkdir(parents=True, exist_ok=True)
    with args.export.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nexport -> {args.export}  ({len(out_rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
