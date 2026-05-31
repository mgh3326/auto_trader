"""Read-only fetch + parse of Binance USD-M Futures 1m monthly klines.

Source: ``data.binance.vision`` public archive (no auth, no secrets). This is
the same host the existing ``research/nautilus_scalping/pit_klines_fetcher.py``
uses for 1d/1h; that fetcher hard-codes ``SUPPORTED_INTERVALS = ("1d","1h")``,
so this spike adds the missing 1m path in isolation rather than touching it.

Downloads one ``<SYMBOL>-1m-<YYYY>-<MM>.zip`` to a gitignored ``data/`` cache and
returns a list of close prices (floats). Pure stdlib — no pandas, no repo deps.
"""

from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

_BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
_DATA_ROOT = Path(__file__).resolve().parent / "data"

# Binance kline CSV column order (no header in the archive files).
_OPEN_TIME, _OPEN, _HIGH, _LOW, _CLOSE = 0, 1, 2, 3, 4


def _zip_url(symbol: str, year: int, month: int) -> str:
    stem = f"{symbol}-1m-{year:04d}-{month:02d}"
    return f"{_BASE}/{symbol}/1m/{stem}.zip"


def fetch_month_closes(
    symbol: str, year: int, month: int
) -> tuple[list[int], list[float]]:
    """Return (open_times_ms, closes) for one symbol-month of 1m USD-M klines.

    Caches the raw zip under ``data/`` (gitignored). Read-only network access.
    """
    _DATA_ROOT.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol}-1m-{year:04d}-{month:02d}"
    zip_path = _DATA_ROOT / f"{stem}.zip"
    if not zip_path.exists():
        url = _zip_url(symbol, year, month)
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (trusted host)
            zip_path.write_bytes(resp.read())

    open_times: list[int] = []
    closes: list[float] = []
    with zipfile.ZipFile(zip_path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            for row in csv.reader(text):
                if not row or not row[_OPEN].replace(".", "", 1).isdigit():
                    continue  # skip a stray header row if the archive adds one
                open_times.append(int(row[_OPEN_TIME]))
                closes.append(float(row[_CLOSE]))
    return open_times, closes
