"""ROB-353 (PR1) — load PIT-trimmed bars from downloaded klines (data half of the PR2 bridge).

Reads the standard Binance kline CSV under ``pit_data_root()/klines/<interval>/<symbol>/``,
emits ``families.Bar`` (ts, high, low, close), trimmed to PIT membership (the manifest's
survivorship-safe ``tradeable_at`` window) with leading/trailing zero-volume bars dropped.
Pure transformation — no network. ``load_panel`` returns per-symbol (ts, close) series for
cross-sectional families.
"""
from __future__ import annotations

import csv
import glob
from pathlib import Path

import families
from artifact_paths import pit_data_root
from pit_universe import PITManifest

_KCOLS = ("open_time", "open", "high", "low", "close", "volume")


def _read_rows(symbol: str, interval: str, root: Path) -> list[tuple[int, float, float, float, float]]:
    """Return sorted, de-duplicated (ts, high, low, close, volume) for a symbol."""
    d = root / "klines" / interval / symbol
    seen: dict[int, tuple[int, float, float, float, float]] = {}
    for path in sorted(glob.glob(str(d / f"{symbol}-{interval}-*.csv"))):
        with open(path, newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if not row or row[0].lower().startswith("open_time"):
                    continue
                try:
                    ts = int(row[0])
                    seen[ts] = (ts, float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                except (ValueError, IndexError):
                    continue
    return [seen[k] for k in sorted(seen)]


def _trim_zero_vol_edges(rows):
    lo, hi = 0, len(rows)
    while lo < hi and rows[lo][4] <= 0.0:
        lo += 1
    while hi > lo and rows[hi - 1][4] <= 0.0:
        hi -= 1
    return rows[lo:hi]


def load_bars(symbol: str, interval: str, manifest: PITManifest, root=None) -> list[families.Bar]:
    root = Path(root) if root else pit_data_root()
    listing = next((x for x in manifest.listings if x.symbol == symbol), None)
    rows = _read_rows(symbol, interval, root)
    if listing is not None:
        rows = [r for r in rows if listing.tradeable_at(r[0])]
    rows = _trim_zero_vol_edges(rows)
    return [families.Bar(ts=r[0], high=r[1], low=r[2], close=r[3]) for r in rows]


def load_panel(symbols, interval: str, manifest: PITManifest, root=None) -> dict[str, list[tuple[int, float]]]:
    root = Path(root) if root else pit_data_root()
    out: dict[str, list[tuple[int, float]]] = {}
    for s in symbols:
        bars = load_bars(s, interval, manifest, root=root)
        if bars:
            out[s] = [(b.ts, b.close) for b in bars]
    return out
