#!/usr/bin/env python3
"""ROB-382 — bounded data fetch for the strat.ninja falsification spike (research only).

Downloads PUBLIC Binance USDⓈ-M klines (data.binance.vision) at the NATIVE timeframes
the ported external strategies use (1m for ClucHAnix, 5m for ichiV1/ElliotV7/VWAP, 1h
for the multi-timeframe informatives). No keys, no auth, no orders, no secrets. Writes
under pit_data_root()/klines/<interval>/<symbol>/ (gitignored). Resumable (skips files
already on disk). NOT committed: raw bars stay out of git.
"""
from __future__ import annotations

import sys

import pit_klines_fetcher as fetcher

SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT")
INTERVALS = ("1m", "5m", "1h")
FROM_MONTH = "2024-01"
TO_MONTH = "2025-12"


def main() -> int:
    for interval in INTERVALS:
        for sym in SYMBOLS:
            summary = fetcher.fetch_months(sym, interval, FROM_MONTH, TO_MONTH, market="um")
            print(
                f"[{interval}] {sym}: downloaded={summary['downloaded']} "
                f"skipped={summary['skipped']} missing={summary['missing']} -> {summary['dir']}",
                flush=True,
            )
    print("FETCH_COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
