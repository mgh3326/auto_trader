"""Local Binance archive -> frozen ROB-1474 daily lists. No network or writes."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from research.crypto_universe.universe import PitUniverse
from research.stock_selection.forward import make_lists

BASELINE = "relative_volatility_20"
HORIZONS = (1, 3, 5, 10)
PANEL_VENUE = "binance_usdt_spot"


def _verified_file(root: Path, record: dict) -> Path:
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("panel path escapes root")
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("panel file hash mismatch")
    return path


def load_binance_panel(root: Path, asof: date) -> pd.DataFrame:
    """Read the delisted-complete crypto_universe artifact, verified before use.

    ``asof`` labels the completed UTC candle, available at next UTC midnight.
    Historical membership is evaluated on each bar's own date, never on today.
    """
    manifest = json.loads((root / "universe/manifest.json").read_text())
    if manifest["corpus_id"] != "crypto-universe-v1":
        raise ValueError("unsupported crypto panel")
    table = manifest["tables"][PANEL_VENUE]
    universe = PitUniverse.load(
        _verified_file(root, table),
        date.fromisoformat(table["coverage_start"]),
        date.fromisoformat(table["coverage_end"]),
    )
    universe.universe_as_of(asof, PANEL_VENUE)  # fail closed outside coverage
    listings = {x.symbol: x for x in universe.for_venue(PANEL_VENUE)}
    index = json.loads(_verified_file(root, manifest["candle_files"]).read_text())
    frames = []
    for record in index[PANEL_VENUE]:
        path = _verified_file(root, record)
        symbol = path.stem
        listing = listings[symbol]
        if listing.is_leveraged_token or listing.is_stable_base:
            continue
        frame = pd.read_parquet(path)
        if len(frame) != record["rows"]:
            raise ValueError("panel row count mismatch")
        frame["date"] = pd.to_datetime(frame.day)
        frame = frame[
            (frame.date.dt.date <= asof) & frame.date.dt.date.map(listing.tradeable_on)
        ].copy()
        frame["symbol"] = symbol
        frames.append(frame)
    if not frames:
        raise ValueError("no Binance spot candles")
    bars = pd.concat(frames, ignore_index=True)
    bars["traded_value"] = bars.quote_volume
    bars["available_at"] = pd.to_datetime(bars.date, utc=True) + timedelta(days=1)
    bars["action"] = False
    benchmark = bars[bars.symbol == "BTCUSDT"].copy()
    if benchmark.empty:
        raise ValueError("BTCUSDT benchmark missing")
    benchmark["symbol"] = "__benchmark__"
    bars = pd.concat([bars, benchmark], ignore_index=True)
    for col in ("open", "high", "low", "close", "volume", "traded_value"):
        bars[col] = pd.to_numeric(bars[col])
    return bars[
        [
            "date",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "traded_value",
            "available_at",
            "action",
        ]
    ]


def daily_lists(root: Path, asof: date, *, horizon: int = 3) -> dict:
    if horizon not in HORIZONS:
        raise ValueError("horizon must be 1, 3, 5 or 10")
    bars = load_binance_panel(root, asof)
    payload = make_lists(
        bars, market="crypto", asof=asof.isoformat(), model=BASELINE, top_n=10
    )
    payload["panel_venue"] = PANEL_VENUE
    payload["selection_basis"] = "ROB-1474 report #2271, descriptive Upbit diagnostic"
    return payload
