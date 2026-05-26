#!/usr/bin/env python3
"""ROB-316 spike — download Binance public aggTrades daily dumps.

Pure stdlib. **Public data only** (data.binance.vision) — no broker keys,
no auth, no order side effects. Each daily ``.zip`` is verified against its
sibling ``.CHECKSUM`` (SHA-256) before extraction.

aggTrades give tick-level fills, which are a far more honest basis for
30/20bps scalping backtests than 1m OHLC (ROB-316 decision doc §6).

Usage:
    python fetch_agg_trades.py --symbol XRPUSDT --market spot \\
        --from-date 2026-05-01 --to-date 2026-05-14 --out data

Markets:
    spot  -> data/spot/daily/aggTrades/<SYMBOL>/...
    um    -> data/futures/um/daily/aggTrades/<SYMBOL>/...   (USDⓈ-M futures)
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

BASE = "https://data.binance.vision/data"
MARKET_PATHS = {
    "spot": "spot/daily/aggTrades",
    "um": "futures/um/daily/aggTrades",
}
_CHUNK = 1 << 16


def _daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _download(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest``. Returns False on 404 (missing day)."""
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
    actual = _sha256(zip_path)
    if actual != expected:
        raise ValueError(
            f"checksum mismatch for {zip_path.name}: "
            f"expected {expected}, got {actual}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Download Binance public aggTrades dumps")
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--market", choices=sorted(MARKET_PATHS), default="spot")
    ap.add_argument("--from-date", required=True, type=_parse_date)
    ap.add_argument("--to-date", required=True, type=_parse_date)
    ap.add_argument("--out", default="data", type=Path)
    args = ap.parse_args()

    if args.from_date > args.to_date:
        ap.error("--from-date must be <= --to-date")

    sub = MARKET_PATHS[args.market]
    out_dir = args.out / args.market / args.symbol
    out_dir.mkdir(parents=True, exist_ok=True)

    downloaded = skipped = missing = 0
    for d in _daterange(args.from_date, args.to_date):
        stem = f"{args.symbol}-aggTrades-{d.isoformat()}"
        csv_path = out_dir / f"{stem}.csv"
        if csv_path.exists():
            print(f"  skip   {stem} (csv exists)")
            skipped += 1
            continue

        url = f"{BASE}/{sub}/{args.symbol}/{stem}.zip"
        zip_path = out_dir / f"{stem}.zip"
        if not _download(url, zip_path):
            print(f"  MISSING {stem} (404 — no dump for this day)")
            missing += 1
            continue

        chk_path = out_dir / f"{stem}.zip.CHECKSUM"
        if _download(f"{url}.CHECKSUM", chk_path):
            _verify(zip_path, chk_path)
            chk_path.unlink()
        else:
            print(f"  warn   {stem}: no CHECKSUM published, skipping verify")

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out_dir)
        zip_path.unlink()
        print(f"  ok     {stem} -> {csv_path.name}")
        downloaded += 1

    print(
        f"\ndone: {downloaded} downloaded, {skipped} skipped, {missing} missing "
        f"-> {out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
