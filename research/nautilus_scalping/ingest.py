#!/usr/bin/env python3
"""ROB-316 spike — ingest Binance aggTrades CSVs into a Nautilus catalog.

Reads the daily aggTrades CSVs produced by ``fetch_agg_trades.py``, converts
them to Nautilus ``TradeTick`` objects via ``TradeTickDataWrangler``, and
writes them (plus the instrument definition) to a ``ParquetDataCatalog``.

Binance aggTrades dumps are headerless with 8 columns; timestamps are in
**microseconds** (Binance switched from ms in 2025).

Usage:
    python ingest.py --data-dir data --market spot --symbol XRPUSDT \\
        --catalog catalog
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import TradeTickDataWrangler

from instruments import xrpusdt_binance

_AGG_COLS = [
    "agg_id",
    "price",
    "quantity",
    "first_id",
    "last_id",
    "transact_time",
    "buyer_maker",
    "best_match",
]

_INSTRUMENTS = {"XRPUSDT": xrpusdt_binance}


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, names=_AGG_COLS)
    df.index = pd.to_datetime(df["transact_time"], unit="us")
    df["trade_id"] = df["agg_id"].astype(str)
    return df[["price", "quantity", "buyer_maker", "trade_id"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest aggTrades into Nautilus catalog")
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--market", default="spot")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--catalog", default="catalog", type=Path)
    args = ap.parse_args()

    if args.symbol not in _INSTRUMENTS:
        ap.error(f"no instrument builder for {args.symbol}; have {list(_INSTRUMENTS)}")

    instrument = _INSTRUMENTS[args.symbol]()
    src_dir = args.data_dir / args.market / args.symbol
    csvs = sorted(src_dir.glob(f"{args.symbol}-aggTrades-*.csv"))
    if not csvs:
        print(f"no CSVs in {src_dir} — run fetch_agg_trades.py first")
        return 1

    args.catalog.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(args.catalog))
    catalog.write_data([instrument])

    wrangler = TradeTickDataWrangler(instrument=instrument)
    total = 0
    for csv in csvs:
        ticks = wrangler.process(_load_csv(csv))
        catalog.write_data(ticks)
        total += len(ticks)
        print(f"  {csv.name}: {len(ticks):>8} ticks")

    # Verify by reading back from the catalog.
    read_back = catalog.trade_ticks(instrument_ids=[instrument.id.value])
    print(f"\nwrote {total} ticks; catalog read-back = {len(read_back)} ticks")
    print(f"instruments in catalog: {[i.id.value for i in catalog.instruments()]}")
    if read_back:
        print(f"first: {read_back[0]}\nlast : {read_back[-1]}")
    return 0 if len(read_back) == total else 2


if __name__ == "__main__":
    sys.exit(main())
