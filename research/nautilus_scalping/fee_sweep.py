#!/usr/bin/env python3
"""ROB-316 — fee/target sensitivity sweep for the breakout scalper.

Question: at what (fee, TP/SL) combinations does the strategy turn net-positive?

Efficiency: fees do NOT change which trades happen (TP/SL are price levels,
fee-independent), but TP/SL DO. So we run the Nautilus engine ONCE per (TP,SL)
combo and recompute net across fee levels analytically — commission scales
linearly with the fee rate, so for a run executed at REF_FEE_BPS per leg:

    net(fee) = realized_pnl + commission_ref * (1 - fee/REF_FEE_BPS)

This is exact (engine commission = fee_rate * (entry+exit notional); we rescale).

Process model: NautilusTrader's Rust logger is a process-global singleton, so a
second BacktestEngine in the same process panics ("logger already initialized").
Each (TP,SL) combo therefore runs in its OWN subprocess (--single worker), and
the driver aggregates the JSON each worker prints.

Usage:
    python fee_sweep.py --catalog catalog --symbol XRPUSDT --trade-size 100
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

REF_FEE_BPS = cost_model.REF_FEE_BPS  # catalog instrument built with 10 bps maker/taker
TP_SL_GRID = [(30, 20), (40, 20), (50, 30), (60, 40), (80, 40), (100, 60), (100, 100)]
FEE_GRID_BPS = [10.0, 7.5, 5.0, 2.0, 1.0, 0.0]
_SENTINEL = "RESULT_JSON "


# --------------------------------------------------------------------------
# worker: run ONE (tp,sl) combo in this process, print per-trade (net, comm)
# --------------------------------------------------------------------------
def _run_single(catalog_path: Path, symbol: str, tp: int, sl: int, trade_size: str):
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.objects import Money
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    from strategy_breakout import BreakoutScalper, BreakoutScalperConfig

    catalog = ParquetDataCatalog(str(catalog_path))
    instrument = next(i for i in catalog.instruments() if i.id.value.startswith(symbol))
    ticks = catalog.trade_ticks(instrument_ids=[instrument.id.value])

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="SWEEP-001", logging=LoggingConfig(log_level="ERROR")
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.HEDGING,
        account_type=AccountType.CASH,
        base_currency=None,
        starting_balances=[Money(1_000_000, USDT)],
    )
    engine.add_instrument(instrument)
    engine.add_data(ticks)
    bar_type = BarType.from_str(f"{instrument.id.value}-1-MINUTE-LAST-INTERNAL")
    engine.add_strategy(
        BreakoutScalper(
            BreakoutScalperConfig(
                instrument_id=instrument.id,
                bar_type=bar_type,
                trade_size=trade_size,
                tp_bps=tp,
                sl_bps=sl,
            )
        )
    )
    engine.run()
    trades = [
        [p.realized_pnl.as_double(), sum(c.as_double() for c in p.commissions())]
        for p in engine.cache.positions_closed()
    ]
    engine.dispose()
    print(_SENTINEL + json.dumps({"tp": tp, "sl": sl, "trades": trades}))


# --------------------------------------------------------------------------
# driver helpers
# --------------------------------------------------------------------------
def _net_at_fee(trades, fee_bps: float) -> tuple[float, int, int]:
    total, wins = 0.0, 0
    for net_ref, comm_ref in trades:
        net = cost_model.net_at_fee(net_ref, comm_ref, fee_bps, REF_FEE_BPS)
        total += net
        if net > 0:
            wins += 1
    return total, len(trades), wins


def _worker_trades(catalog: Path, symbol: str, tp: int, sl: int, size: str):
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--single", "--tp", str(tp),
         "--sl", str(sl), "--catalog", str(catalog), "--symbol", symbol,
         "--trade-size", size],
        capture_output=True, text=True, env=os.environ,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            return json.loads(line[len(_SENTINEL):])["trades"]
    raise RuntimeError(
        f"worker {tp}/{sl} produced no result.\nstderr tail:\n{proc.stderr[-600:]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Fee/target sensitivity sweep")
    ap.add_argument("--catalog", default="catalog", type=Path)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--trade-size", default="100")
    ap.add_argument("--export", default="results/fee_sweep.csv", type=Path)
    ap.add_argument("--single", action="store_true", help="worker: run one combo")
    ap.add_argument("--tp", type=int)
    ap.add_argument("--sl", type=int)
    args = ap.parse_args()

    if args.single:
        _run_single(args.catalog, args.symbol, args.tp, args.sl, args.trade_size)
        return 0

    print(f"sweep: {args.symbol}, size={args.trade_size}")
    print(f"TP/SL combos: {len(TP_SL_GRID)} (subprocess each), fees: {FEE_GRID_BPS}\n")

    rows = []
    head = f"{'TP/SL':>9} {'trades':>7} " + " ".join(f"{f:>8.1f}" for f in FEE_GRID_BPS)
    print(head + "   (cells = NET PnL USDT; per-leg fee bps in header)")
    print("-" * len(head))

    for tp, sl in TP_SL_GRID:
        trades = _worker_trades(args.catalog, args.symbol, tp, sl, args.trade_size)
        cells = []
        for fee in FEE_GRID_BPS:
            net, n, wins = _net_at_fee(trades, fee)
            cells.append(f"{net:>+8.1f}")
            rows.append({
                "tp_bps": tp, "sl_bps": sl, "fee_bps_per_leg": fee, "trades": n,
                "net_wins": wins,
                "win_rate_pct": round(100 * wins / n, 1) if n else 0.0,
                "net_pnl_usdt": round(net, 2),
            })
        print(f"{f'{tp}/{sl}':>9} {len(trades):>7} " + " ".join(cells))

    print("\nbreak-even frontier (max per-leg fee bps with NET > 0):")
    for tp, sl in TP_SL_GRID:
        positive = [r["fee_bps_per_leg"] for r in rows
                    if r["tp_bps"] == tp and r["sl_bps"] == sl and r["net_pnl_usdt"] > 0]
        verdict = f"fee <= {max(positive):.1f} bps" if positive else "NEVER net-positive"
        print(f"  TP/SL {tp}/{sl}: {verdict}")

    args.export.parent.mkdir(parents=True, exist_ok=True)
    with args.export.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nexport -> {args.export}  ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
