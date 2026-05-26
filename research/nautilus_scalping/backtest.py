#!/usr/bin/env python3
"""ROB-316 spike — run the breakout scalper backtest on ingested tick data.

Loads ``TradeTick`` data from the Nautilus catalog, aggregates 1m bars
internally, runs ``BreakoutScalper`` on a spot CASH venue, and reports
conservative net-after-cost metrics (gross is separated from net).

Usage:
    python backtest.py --catalog catalog --symbol XRPUSDT --trade-size 100
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from strategy_breakout import BreakoutScalper, BreakoutScalperConfig


def _run(catalog_path: Path, symbol: str, trade_size: str, balance: int):
    catalog = ParquetDataCatalog(str(catalog_path))
    instruments = catalog.instruments()
    instrument = next(i for i in instruments if i.id.value.startswith(symbol))
    ticks = catalog.trade_ticks(instrument_ids=[instrument.id.value])
    if not ticks:
        print("no ticks in catalog — run ingest.py first")
        sys.exit(1)

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="BACKTESTER-001",
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        # HEDGING so each flat-to-flat round trip is a discrete Position
        # (clean per-trade analytics); economics match NETTING for this
        # long-only, flat-between-trades scalper.
        oms_type=OmsType.HEDGING,
        account_type=AccountType.CASH,
        base_currency=None,
        starting_balances=[Money(balance, USDT)],
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
            )
        )
    )
    print(f"running backtest: {len(ticks)} ticks, {symbol}, size={trade_size}")
    engine.run()
    return engine, instrument


def _trade_rows(engine, symbol: str) -> list[dict]:
    """Trade-level rows mirroring scalp_trade_analytics concepts (export only)."""
    rows = []
    for p in engine.cache.positions_closed():
        net = p.realized_pnl.as_double()
        fee = sum(c.as_double() for c in p.commissions())
        gross = net + fee
        entry_px = float(p.avg_px_open)
        exit_px = float(p.avg_px_close)
        qty = float(p.peak_qty)
        notional = entry_px * qty
        ret_bps = (net / notional * 10_000) if notional else 0.0
        rows.append(
            {
                "symbol": symbol,
                "product": "spot",
                "side": p.entry.name,  # BUY = long entry
                "qty": qty,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "entry_notional_usdt": round(notional, 6),
                "fee_usdt": round(fee, 6),
                "gross_pnl_usdt": round(gross, 6),
                "net_pnl_usdt": round(net, 6),
                "net_return_bps": round(ret_bps, 2),
                "holding_seconds": int(p.duration_ns / 1e9),
                # tp/sl not tracked on the position; infer from direction (approx).
                "exit_reason": "take_profit" if net > 0 else "stop_loss",
            }
        )
    return rows


def _max_drawdown(net_pnls: list[float]) -> float:
    equity, peak, mdd = 0.0, 0.0, 0.0
    for pnl in net_pnls:
        equity += pnl
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
    return mdd


def _report(engine, instrument, symbol: str, export_path: Path | None) -> None:
    rows = _trade_rows(engine, symbol)
    if not rows:
        print("\nno closed trades")
        return

    net = [r["net_pnl_usdt"] for r in rows]
    gross = [r["gross_pnl_usdt"] for r in rows]
    fees = [r["fee_usdt"] for r in rows]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    holds = [r["holding_seconds"] for r in rows]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    print("\n" + "=" * 48)
    print(f"BACKTEST RESULTS — {symbol} (spot, tick-level)")
    print("=" * 48)
    print(f"trades:            {len(rows)}")
    print(f"win_rate:          {len(wins) / len(rows) * 100:.1f}%  ({len(wins)}/{len(rows)})")
    print(f"gross_pnl_usdt:    {sum(gross):+.4f}")
    print(f"total_fees_usdt:   {sum(fees):.4f}")
    print(f"NET_pnl_usdt:      {sum(net):+.4f}")
    print(f"profit_factor:     {(gross_win / gross_loss) if gross_loss else float('inf'):.2f}")
    print(f"avg_net_per_trade: {statistics.mean(net):+.4f} usdt")
    print(f"avg_net_return:    {statistics.mean([r['net_return_bps'] for r in rows]):+.2f} bps")
    print(f"avg_win / avg_loss:{(statistics.mean(wins) if wins else 0):+.4f} / "
          f"{(statistics.mean(losses) if losses else 0):+.4f} usdt")
    print(f"max_drawdown_usdt: {_max_drawdown(net):.4f}")
    print(f"avg_holding_sec:   {statistics.mean(holds):.0f}")
    print("=" * 48)
    print("NOTE: gross vs net separated; net is after taker fees. "
          "30/20bps TP/SL means fees dominate — read NET only.")

    if export_path:
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with export_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\ntrade-level export -> {export_path}  ({len(rows)} rows)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run breakout scalper backtest")
    ap.add_argument("--catalog", default="catalog", type=Path)
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--trade-size", default="100")
    ap.add_argument("--balance", default=50_000, type=int)
    ap.add_argument("--export", default="results/trades.csv", type=Path)
    args = ap.parse_args()

    engine, instrument = _run(args.catalog, args.symbol, args.trade_size, args.balance)
    _report(engine, instrument, args.symbol, args.export)
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
