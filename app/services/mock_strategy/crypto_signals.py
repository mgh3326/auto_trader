"""Local Binance archive -> frozen ROB-1474 daily lists. No network or writes."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from research.crypto_universe.universe import PitUniverse, crypto_exclusion_reason
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
    exclusion_reasons = {}
    for record in index[PANEL_VENUE]:
        path = _verified_file(root, record)
        symbol = path.stem
        listing = listings[symbol]
        frame = pd.read_parquet(path)
        if len(frame) != record["rows"]:
            raise ValueError("panel row count mismatch")
        frame["date"] = pd.to_datetime(frame.day)
        completed = frame[frame.date.dt.date <= asof].copy()
        reason = crypto_exclusion_reason(
            listing.base_asset,
            (
                (stamp.date(), float(close))
                for stamp, close in zip(completed.date, completed.close, strict=True)
            ),
            cutoff=asof,
        )
        if reason:
            exclusion_reasons[symbol] = reason
            continue
        frame = completed[completed.date.dt.date.map(listing.tradeable_on)].copy()
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
    result = bars[
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
    result.attrs["exclusion_reasons"] = exclusion_reasons
    return result


def daily_lists(
    root: Path | None, asof: date, *, horizon: int = 3, cache_root: Path | None = None
) -> dict:
    if horizon not in HORIZONS:
        raise ValueError("horizon must be 1, 3, 5 or 10")
    meta: dict | None = None
    if cache_root is not None:
        from research.stock_selection.io import read_bars

        bars, meta = read_bars(cache_root, "crypto")
        if meta.get("producer") != "mock-data-refresh/v1":
            raise ValueError("crypto cache requires mock_data_refresh provenance")
        if date.fromisoformat(meta["observation_date"]) > asof + timedelta(days=1):
            raise ValueError("future universe observation")
        if date.fromisoformat(meta["data_cutoff"]) != asof:
            raise ValueError("universe cutoff must match requested crypto candle")
    else:
        if root is None:
            raise ValueError("panel root required")
        bars = load_binance_panel(root, asof)
    exclusion_reasons = dict(bars.attrs.get("exclusion_reasons", {}))
    payload = make_lists(
        bars, market="crypto", asof=asof.isoformat(), model=BASELINE, top_n=10
    )
    if meta is not None:
        exclusion_reasons = dict(meta.get("excluded", {}))
        payload["universe_observed_at"] = meta["fetched_at"]
        payload["prediction_at"] = max(
            pd.Timestamp(payload["prediction_at"]), pd.Timestamp(meta["fetched_at"])
        ).isoformat()
    payload["panel_venue"] = PANEL_VENUE
    payload["exclusion_reasons"] = exclusion_reasons
    payload["selection_basis"] = "ROB-1474 report #2271, descriptive Upbit diagnostic"
    return payload
