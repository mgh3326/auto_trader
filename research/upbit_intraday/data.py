"""Load the hashed 15-minute candle files into a dense slot panel (hk 6035).

The loader refuses anything that does not match ``manifest.json``: a changed
file hash, a window other than the frozen one, or a market listed without its
file. Slots without a candle stay NaN; nothing is filled.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import rules as R
from .fetch import BAR_SECONDS, MANIFEST_FORMAT


class DatasetError(RuntimeError):
    pass


def manifest_sha256(root: Path) -> str:
    return hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()


def _window_epoch(manifest: dict[str, Any]) -> tuple[int, int]:
    w = manifest["window"]

    def parse(text: str) -> int:
        return int(
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone(UTC)
            .timestamp()
        )

    return parse(w["start_utc"]), parse(w["end_exclusive_utc"])


def load_panel(
    root: Path,
    *,
    start_epoch: int | None = None,
    end_exclusive_epoch: int | None = None,
    only: list[str] | None = None,
) -> tuple[R.Panel, dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("manifest_format") != MANIFEST_FORMAT:
        raise DatasetError("unexpected manifest format")
    if manifest.get("failures"):
        raise DatasetError(
            f"manifest records fetch failures: {sorted(manifest['failures'])}"
        )
    start, end = _window_epoch(manifest)
    want_start = start_epoch if start_epoch is not None else R.epoch(R.DATA_START_UTC)
    want_end = (
        end_exclusive_epoch
        if end_exclusive_epoch is not None
        else R.epoch(R.DATA_END_EXCLUSIVE_UTC)
    )
    if (start, end) != (want_start, want_end):
        raise DatasetError("manifest window differs from the frozen window")
    slot0 = start // BAR_SECONDS
    n = (end - start) // BAR_SECONDS
    market_all = (root / manifest["market_list"]["file"]).read_bytes()
    if hashlib.sha256(market_all).hexdigest() != manifest["market_list"]["sha256"]:
        raise DatasetError("market list hash mismatch")

    names = sorted(m for m, meta in manifest["markets"].items() if meta["rows"] > 0)
    if only is not None:
        names = [m for m in names if m in set(only)]
    arrays = {k: np.full((len(names), n), np.nan) for k in "ohlcvq"}
    for i, market in enumerate(names):
        meta = manifest["markets"][market]
        blob = (root / meta["file"]).read_bytes()
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            raise DatasetError(f"{market}: file hash mismatch")
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as gz:
            frame = pd.read_csv(gz)
        if len(frame) != meta["rows"]:
            raise DatasetError(f"{market}: row count mismatch")
        stamps = pd.to_datetime(frame["time_utc"], utc=True)
        # pandas 3 keeps datetime64[s|ms|us|ns] resolution; normalise to seconds.
        secs = stamps.dt.as_unit("s").astype("int64").to_numpy()
        slots = secs // BAR_SECONDS - slot0
        if secs.min() < start or secs.max() >= end or (secs % BAR_SECONDS).any():
            raise DatasetError(f"{market}: candle outside window or off grid")
        if len(np.unique(slots)) != len(slots):
            raise DatasetError(f"{market}: duplicate candle slot")
        for key, col in zip(
            "ohlcvq",
            ("open", "high", "low", "close", "volume", "quote_volume"),
            strict=True,
        ):
            arrays[key][i, slots] = frame[col].to_numpy(dtype=float)
    panel = R.Panel(
        markets=tuple(names),
        slot0=slot0,
        o=arrays["o"],
        h=arrays["h"],
        l=arrays["l"],
        c=arrays["c"],
        v=arrays["v"],
        q=arrays["q"],
    )
    return panel, manifest


def warning_markets(root: Path) -> frozenset[str]:
    """KRW markets flagged warning or caution in the market list at fetch time.

    Not point in time: history of these flags does not exist in the data.
    """
    payload = json.loads((root / "market_all.json").read_text())
    flagged: set[str] = set()
    for item in payload:
        market = item.get("market", "")
        if not market.startswith("KRW-"):
            continue
        event = item.get("market_event") or {}
        caution = event.get("caution") or {}
        if event.get("warning") or any(bool(v) for v in caution.values()):
            flagged.add(market)
    return frozenset(flagged)
