"""Generate a synthetic crypto_universe artifact for offline CLI acceptance."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta
from pathlib import Path

from research.crypto_universe.build import VenueResult, write_candles, write_outputs
from research.crypto_universe.universe import derive_listing


def build_fixture(root: Path) -> None:
    start, end = date(2022, 12, 1), date(2023, 12, 31)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    listings, files, markets = [], [], {}
    for n, symbol in enumerate(["BTCUSDT", *[f"COIN{i}USDT" for i in range(24)]]):
        last = days[:-5] if n == 24 else days
        rows = []
        for i, day in enumerate(last):
            close = 50 + n + 0.01 * i + math.sin(i * (0.15 + n * 0.01)) * (n + 1) * 0.05
            rows.append(
                {
                    "day": day,
                    "open": f"{close:.8f}",
                    "high": f"{close + 1:.8f}",
                    "low": f"{close - 1:.8f}",
                    "close": f"{close:.8f}",
                    "volume": "10000",
                    "quote_volume": f"{close * 10000:.8f}",
                    "trades": 100,
                }
            )
        files.extend(write_candles(root, "binance_usdt_spot", symbol, rows))
        listings.append(
            derive_listing(
                venue="binance_usdt_spot",
                symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
                traded_days=last,
                exchange_status="BREAK" if n == 24 else "TRADING",
                coverage_end=end,
                source="synthetic_offline_fixture",
            )
        )
        markets[symbol] = {
            "step": "0.001",
            "min_qty": "0.001",
            "min_notional": "5",
            "price": rows[-1]["close"],
        }
    write_outputs(
        root,
        {
            "binance_usdt_spot": VenueResult(
                tuple(listings), start, end, tuple(files), (), {}
            )
        },
        {"fixture": True},
    )
    (root / "market.json").write_text(json.dumps(markets, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    build_fixture(parser.parse_args().root)
