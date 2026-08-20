"""Offline loaders for the three frozen bar corpora.

Local parquet only. No network, no operating DB, no broker. The sealed
2025-01-01..2026-07-31 holdout partitions are never listed or opened; every
loader is rooted at the main ``dataset/`` tree and asserts it.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass

import pandas as pd

from research.buy_gate_ab_backtest.preregistration import ADDENDUM

_FORBIDDEN_PATH_PARTS = ("/holdout/", "/holdout-")


@dataclass(frozen=True, slots=True)
class MarketPanel:
    """Long-format bars for one market, sorted by (symbol, session_date)."""

    market: str
    frame: pd.DataFrame  # symbol, session_date, open, high, low, close, volume, value
    sessions: tuple[pd.Timestamp, ...]
    corpus_id: str
    files_read: int


def _assert_not_holdout(paths: list[str]) -> None:
    for path in paths:
        lowered = path.replace(os.sep, "/").lower()
        if any(part in lowered for part in _FORBIDDEN_PATH_PARTS):
            raise RuntimeError(f"refusing to read sealed holdout path: {path}")


def _finalize(
    frame: pd.DataFrame, *, market: str, corpus_id: str, files_read: int
) -> MarketPanel:
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    # A corpus row with a zero OHLC is a non-traded placeholder, not a bar.
    frame = frame[(frame["close"] > 0) & (frame["high"] > 0) & (frame["low"] > 0)]
    frame = frame.sort_values(["symbol", "session_date"], kind="mergesort")
    frame = frame.reset_index(drop=True)
    frame["value"] = frame["close"] * frame["volume"]
    sessions = tuple(sorted(pd.unique(frame["session_date"])))
    return MarketPanel(
        market=market,
        frame=frame,
        sessions=sessions,
        corpus_id=corpus_id,
        files_read=files_read,
    )


def load_kr() -> MarketPanel:
    spec = ADDENDUM["corpora"]["kr"]
    paths = sorted(glob.glob(os.path.join(spec["root"], "**", "*.parquet"), recursive=True))
    _assert_not_holdout(paths)
    if not paths:
        raise RuntimeError(f"kr corpus is empty at {spec['root']}")
    parts = []
    for path in paths:
        frame = pd.read_parquet(
            path, columns=["session", "ticker", "open", "high", "low", "close", "volume"]
        )
        parts.append(frame)
    frame = pd.concat(parts, ignore_index=True)
    frame = frame.rename(columns={"session": "session_date", "ticker": "symbol"})
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    lo, hi = spec["window"]
    frame = frame[
        (frame["session_date"] >= pd.Timestamp(lo))
        & (frame["session_date"] <= pd.Timestamp(hi))
    ]
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    return _finalize(
        frame, market="kr", corpus_id=spec["corpus_id"], files_read=len(paths)
    )


def load_us() -> MarketPanel:
    spec = ADDENDUM["corpora"]["us"]
    paths = sorted(glob.glob(os.path.join(spec["root"], "**", "*.parquet"), recursive=True))
    _assert_not_holdout(paths)
    if not paths:
        raise RuntimeError(f"us corpus is empty at {spec['root']}")
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    frame["session_date"] = pd.to_datetime(frame["session_date"])
    lo, hi = spec["window"]
    frame = frame[
        (frame["session_date"] >= pd.Timestamp(lo))
        & (frame["session_date"] <= pd.Timestamp(hi))
    ]
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    return _finalize(
        frame, market="us", corpus_id=spec["corpus_id"], files_read=len(paths)
    )


def load_crypto(venue: str) -> MarketPanel:
    spec = ADDENDUM["corpora"]["crypto"]
    if venue not in spec["venues"]:
        raise ValueError(f"unknown crypto venue: {venue}")
    root = os.path.join(spec["root"], f"venue={venue}")
    paths = sorted(
        path
        for path in glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True)
        if "__1d__" in os.path.basename(path)
    )
    _assert_not_holdout(paths)
    if not paths:
        raise RuntimeError(f"crypto corpus is empty at {root}")
    parts = []
    for path in paths:
        frame = pd.read_parquet(
            path,
            columns=[
                "symbol",
                "frequency",
                "open_time_utc",
                "open",
                "high",
                "low",
                "close",
                "base_volume",
            ],
        )
        parts.append(frame[frame["frequency"] == "1d"])
    frame = pd.concat(parts, ignore_index=True)
    frame = frame.rename(columns={"open_time_utc": "session_date", "base_volume": "volume"})
    frame["session_date"] = pd.to_datetime(frame["session_date"], utc=True).dt.tz_localize(None)
    frame["session_date"] = frame["session_date"].dt.normalize()
    frame = frame.drop_duplicates(subset=["symbol", "session_date"], keep="last")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    return _finalize(
        frame,
        market=f"crypto_{venue}",
        corpus_id=spec["corpus_id"],
        files_read=len(paths),
    )
