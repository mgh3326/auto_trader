#!/usr/bin/env python3
"""ROB-353 (PR1) — download Binance USDⓈ-M public klines dumps (1d, 1h, 5m).

Pure stdlib. PUBLIC data only (data.binance.vision) — no keys, no auth, no orders.
Each archive is verified against its sibling ``.CHECKSUM`` when published. Writes
under ``artifact_paths.pit_data_root()`` (gitignored). No secrets are printed.

Usage:
    uv run --no-project python pit_klines_fetcher.py --symbol EOSUSDT \\
        --interval 1d --from-month 2023-01 --to-month 2024-01
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from artifact_paths import pit_data_root

BASE = "https://data.binance.vision/data"
# ROB-382: added 1m/5m/15m so external-strategy ports run at their NATIVE timeframe
# (timeframe-faithful falsification, not short-horizon coercion). 1d/1h predate ROB-353.
# Superset of the ROB-353 base ("1d", "1h", "5m").
SUPPORTED_INTERVALS = ("1m", "5m", "15m", "1h", "1d")
_CHUNK = 1 << 16


def kline_url(symbol, interval, year, month, market="um", cadence="monthly", day=None):
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"unsupported interval {interval!r}; expected {SUPPORTED_INTERVALS}"
        )
    if cadence == "monthly":
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
        sub = f"futures/{market}/monthly/klines/{symbol}/{interval}"
    elif cadence == "daily":
        if day is None:
            raise ValueError("daily cadence requires day")
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}-{day:02d}"
        sub = f"futures/{market}/daily/klines/{symbol}/{interval}"
    else:
        raise ValueError(f"unknown cadence {cadence!r}")
    return f"{BASE}/{sub}/{stem}.zip"


def _download(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:
            while chunk := resp.read(_CHUNK):
                fh.write(chunk)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _verify(zip_path: Path, checksum_path: Path) -> None:
    expected = checksum_path.read_text().split()[0].strip().lower()
    if _sha256(zip_path) != expected:
        raise ValueError(f"checksum mismatch for {zip_path.name}")


def _months(from_month: str, to_month: str):
    y0, m0 = int(from_month[:4]), int(from_month[5:7])
    y1, m1 = int(to_month[:4]), int(to_month[5:7])
    cur = y0 * 12 + (m0 - 1)
    end = y1 * 12 + (m1 - 1)
    while cur <= end:
        yield cur // 12, cur % 12 + 1
        cur += 1


def fetch_months(
    symbol, interval, from_month, to_month, market="um", out_root=None
) -> dict:
    out_root = Path(out_root) if out_root else pit_data_root()
    out_dir = out_root / "klines" / interval / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = missing = 0
    for year, month in _months(from_month, to_month):
        stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
        csv_path = out_dir / f"{stem}.csv"
        if csv_path.exists():
            skipped += 1
            continue
        url = kline_url(symbol, interval, year, month, market=market, cadence="monthly")
        zip_path = out_dir / f"{stem}.zip"
        if not _download(url, zip_path):
            missing += 1
            continue
        chk_path = out_dir / f"{stem}.zip.CHECKSUM"
        if _download(f"{url}.CHECKSUM", chk_path):
            _verify(zip_path, chk_path)
            chk_path.unlink()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out_dir)
        zip_path.unlink()
        downloaded += 1
    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": missing,
        "dir": str(out_dir),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Download Binance USDⓈ-M public klines (1d/1h/5m)"
    )
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", choices=SUPPORTED_INTERVALS, required=True)
    ap.add_argument("--from-month", required=True, help="YYYY-MM")
    ap.add_argument("--to-month", required=True, help="YYYY-MM")
    ap.add_argument("--market", default="um")
    args = ap.parse_args(argv)
    summary = fetch_months(
        args.symbol, args.interval, args.from_month, args.to_month, args.market
    )
    print(
        f"done: {summary['downloaded']} downloaded, {summary['skipped']} skipped, "
        f"{summary['missing']} missing -> {summary['dir']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
