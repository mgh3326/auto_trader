#!/usr/bin/env python3
"""ROB-320 — generic subprocess backtest runner -> list of Trade tuples.

One BacktestEngine per subprocess (Nautilus's Rust logger is a process-global
singleton). The parent calls ``run(...)``; the ``--single`` child runs ONE
(strategy, params) on one symbol and prints a RESULT_JSON line of trades at the
reference fee. Net at any fee is recomputed by validated_gate analytically.

Nautilus is imported LAZILY inside the child only, so importing this module in
the pure test layer is cheap and venv-free at import time.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from validated_gate import Trade

_SENTINEL = "RESULT_JSON "


def _window_bounds_ns(window_from: str, window_to: str) -> tuple[int | None, int | None]:
    """Parse YYYY-MM-DD window edges to epoch-ns; 'to' date inclusive ([lo, hi)).

    Stdlib-only (keeps this module venv-free at import) and integer-exact; matches
    discovery.data.window_bounds_ns so the Nautilus path and the discovery path
    interpret --window-from/--window-to identically.
    """
    def _to_ns(s: str, *, plus_one_day: bool = False) -> int:
        dt = datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        return int((dt + timedelta(days=1) if plus_one_day else dt).timestamp()) * 1_000_000_000

    lo = _to_ns(window_from) if window_from and window_from.strip() else None
    hi = _to_ns(window_to, plus_one_day=True) if window_to and window_to.strip() else None
    return lo, hi


def _filter_ticks_window(ticks, ts_from: int | None, ts_to: int | None):
    """Keep ticks with ts_event in [ts_from, ts_to); None = unbounded. Applied BEFORE
    engine.add_data so the window constrains processed data, not just metadata."""
    if ts_from is None and ts_to is None:
        return ticks
    return [t for t in ticks
            if (ts_from is None or t.ts_event >= ts_from)
            and (ts_to is None or t.ts_event < ts_to)]


def _run_single(catalog: Path, symbol: str, strategy: str, params: dict, trade_size: str,
                window_from: str = "", window_to: str = "") -> None:
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog_obj = ParquetDataCatalog(str(catalog))
    instrument = next(i for i in catalog_obj.instruments() if i.id.value.startswith(symbol))
    ticks = catalog_obj.trade_ticks(instrument_ids=[instrument.id.value])
    lo, hi = _window_bounds_ns(window_from, window_to)
    ticks = _filter_ticks_window(ticks, lo, hi)  # real window constraint (pre-add_data)

    execution_mode = str(params.get("execution_mode", "taker"))
    if execution_mode == "maker":
        # ZERO-fee instrument: realized_pnl is pure (fee-free) gross price P&L. maker_fill
        # then applies the REAL Binance Demo per-leg fees analytically (maker 2bps on the
        # entry + the TP leg, taker 4bps on the SL leg). Keeps the gate-feeding net exact
        # without relying on a single Nautilus commission rate for the mixed maker/taker mix.
        instrument = CurrencyPair(
            instrument_id=instrument.id, raw_symbol=instrument.raw_symbol,
            base_currency=instrument.base_currency, quote_currency=instrument.quote_currency,
            price_precision=instrument.price_precision, size_precision=instrument.size_precision,
            price_increment=instrument.price_increment, size_increment=instrument.size_increment,
            lot_size=instrument.lot_size, max_quantity=instrument.max_quantity,
            min_quantity=instrument.min_quantity, max_notional=instrument.max_notional,
            min_notional=instrument.min_notional, max_price=instrument.max_price,
            min_price=instrument.min_price, margin_init=instrument.margin_init,
            margin_maint=instrument.margin_maint,
            maker_fee=Decimal("0"), taker_fee=Decimal("0"),
            ts_event=0, ts_init=0)

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id="ROB320-001", logging=LoggingConfig(log_level="ERROR")))

    # Both modes use ROB-320's venue: HEDGING is fine because every exit is an explicit
    # market close_all_positions (OMS-agnostic) — maker mode no longer rests a TP limit,
    # so the earlier counter-position problem is gone. Taker reproduces ROB-320 exactly
    # (789 trades, net@10bps -209.71).
    engine.add_venue(venue=Venue("BINANCE"), oms_type=OmsType.HEDGING,
                     account_type=AccountType.CASH, base_currency=None,
                     starting_balances=[Money(10_000_000, USDT)])
    engine.add_instrument(instrument)
    engine.add_data(ticks)
    bar_type = BarType.from_str(f"{instrument.id.value}-1-MINUTE-LAST-INTERNAL")

    if strategy == "micro_breakout":
        from strategy_breakout import BreakoutScalper, BreakoutScalperConfig
        strat = BreakoutScalper(BreakoutScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type, trade_size=trade_size,
            tp_bps=int(params.get("tp_bps", 30)), sl_bps=int(params.get("sl_bps", 20))))
    elif strategy == "meanrev_zscore_fade":
        from strategy_meanrev import MeanRevScalper, MeanRevScalperConfig
        strat = MeanRevScalper(MeanRevScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type, trade_size=trade_size,
            lookback=int(params.get("lookback", 20)), z_entry=str(params.get("z_entry", "2.0")),
            tp_bps=int(params.get("tp_bps", 30)), sl_bps=int(params.get("sl_bps", 30)),
            require_vol=bool(params.get("require_vol", True)),
            execution_mode=execution_mode))
    elif strategy == "random_entry":
        from strategy_random import RandomScalper, RandomScalperConfig
        strat = RandomScalper(RandomScalperConfig(
            instrument_id=instrument.id, bar_type=bar_type, trade_size=trade_size,
            entry_prob=float(params.get("entry_prob", 0.02)), seed=int(params.get("seed", 42)),
            tp_bps=int(params.get("tp_bps", 30)), sl_bps=int(params.get("sl_bps", 30))))
    else:
        raise SystemExit(f"unknown strategy {strategy}")

    engine.add_strategy(strat)
    engine.run()

    if execution_mode == "maker":
        payload = {
            "execution_mode": "maker",
            "records": list(strat.records),
            "entries_attempted": int(strat.entries_attempted),
            "entries_filled": int(strat.entries_filled),
        }
        engine.dispose()
        print(_SENTINEL + json.dumps(payload))
        return

    trades = []
    for p in engine.cache.positions_closed():
        net_ref = p.realized_pnl.as_double()
        comm_ref = sum(c.as_double() for c in p.commissions())
        notional = float(p.avg_px_open) * float(p.peak_qty)
        trades.append([net_ref, comm_ref, notional, int(p.ts_opened)])
    engine.dispose()
    print(_SENTINEL + json.dumps({"trades": trades}))


def run(catalog: Path, symbol: str, strategy: str, params: dict, trade_size: str = "100",
        window_from: str = "", window_to: str = "") -> list[Trade]:
    # We must explicitly use the python interpreter inside our research .venv and pass PYTHONPATH
    venv_python = sys.executable
    cmd = [venv_python, os.path.abspath(__file__), "--single",
           "--catalog", str(catalog), "--symbol", symbol, "--strategy", strategy,
           "--params", json.dumps(params), "--trade-size", trade_size,
           "--window-from", window_from, "--window-to", window_to]

    # Propagate the current PYTHONPATH so we can resolve the app package inside the child process
    env = dict(os.environ)
    if "PYTHONPATH" not in env:
        env["PYTHONPATH"] = "../.."
    else:
        env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + "../.."

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            raw = json.loads(line[len(_SENTINEL):])["trades"]
            return [Trade(net_ref_pnl=r[0], commission_ref=r[1], notional=r[2], ts_opened=r[3])
                    for r in raw]
    raise RuntimeError(f"runner {strategy} on {symbol} failed:\n{proc.stderr[-800:]}")


def run_maker(catalog: Path, symbol: str, params: dict, trade_size: str = "100",
              window_from: str = "", window_to: str = ""):
    """Run the maker re-sim; return (records, attempted, filled).

    records are maker_fill.MakerTradeRecord; attempted-filled = missed fills."""
    from maker_fill import MakerTradeRecord
    p = dict(params)
    p["execution_mode"] = "maker"
    venv_python = sys.executable
    cmd = [venv_python, os.path.abspath(__file__), "--single", "--catalog", str(catalog),
           "--symbol", symbol, "--strategy", "meanrev_zscore_fade",
           "--params", json.dumps(p), "--trade-size", trade_size,
           "--window-from", window_from, "--window-to", window_to]
    env = dict(os.environ)
    env["PYTHONPATH"] = env.get("PYTHONPATH", "") + os.pathsep + "../.."
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    for line in proc.stdout.splitlines():
        if line.startswith(_SENTINEL):
            data = json.loads(line[len(_SENTINEL):])
            recs = [MakerTradeRecord(
                gross=r["gross"], entry_notional=r["entry_notional"],
                exit_notional=r["exit_notional"], ts_opened=r["ts"],
                filled=r["filled"], tp_hit=r["tp_hit"],
                adverse_excursion_bps=r["adverse_bps"])
                for r in data["records"]]
            return recs, data["entries_attempted"], data["entries_filled"]
    raise RuntimeError(f"maker runner {symbol} failed:\n{proc.stderr[-800:]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--params", default="{}")
    ap.add_argument("--trade-size", default="100")
    ap.add_argument("--window-from", default="")
    ap.add_argument("--window-to", default="")
    args = ap.parse_args()
    if args.single:
        _run_single(args.catalog, args.symbol, args.strategy, json.loads(args.params),
                    args.trade_size, args.window_from, args.window_to)
        return 0
    raise SystemExit("backtest_runner is a library; use run() or --single")


if __name__ == "__main__":
    sys.exit(main())
