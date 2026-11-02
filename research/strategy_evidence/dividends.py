"""Cash distributions of the parking instruments: a fetch step and a verified reader.

The ROB-1439 price snapshot keeps closes and splits but not dividends, and SGOV and
BIL return almost all of their yield as monthly distributions. This snapshot adds the
distributions from Yahoo Finance, unauthenticated, in the same hashed form: a new
fetch goes to a new directory because a provider can revise its history.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from pathlib import Path

from research.trade_retro.export import ExportIntegrityError, sha256_file

MANIFEST = "dividends_manifest.json"
#: The closed parking tuple, with the Yahoo ticker for each.
TICKERS = {
    ("us", "SGOV"): "SGOV",
    ("us", "BIL"): "BIL",
    ("kr", "459580"): "459580.KS",
    ("kr", "357870"): "357870.KS",
}


def fetch_dividends(out_dir: Path, start: date, end_exclusive: date) -> dict:
    import yfinance as yf

    out_dir = Path(out_dir).expanduser()
    if (out_dir / MANIFEST).exists():
        raise FileExistsError(f"{out_dir} already holds a dividends snapshot")
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = {}
    for (market, symbol), ticker in sorted(TICKERS.items()):
        series = yf.Ticker(ticker).dividends
        rows = [
            {"date": stamp.date().isoformat(), "amount": repr(float(value))}
            for stamp, value in series.items()
            if start <= stamp.date() < end_exclusive
        ]
        rel = f"{market}/{symbol}.csv"
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "amount"])
            writer.writeheader()
            writer.writerows(rows)
        entries[f"{market}/{symbol}"] = {
            "file": rel,
            "source": f"yahoo:{ticker}",
            "rows": len(rows),
            "sha256": sha256_file(path),
        }
    manifest = {
        "schema": "strategy-evidence-dividends/v1",
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end_exclusive": end_exclusive.isoformat(),
        "entries": entries,
    }
    (out_dir / MANIFEST).write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8"
    )
    return manifest


def load_dividends(
    root: Path,
) -> tuple[dict[tuple[str, str], list[tuple[date, float]]], str]:
    """Read the snapshot, refusing any file whose hash differs from the manifest."""
    root = Path(root).expanduser()
    path = root / MANIFEST
    if not path.is_file():
        raise ExportIntegrityError(f"no {MANIFEST} in {root}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for key, entry in manifest["entries"].items():
        file_path = root / entry["file"]
        if not file_path.is_file() or sha256_file(file_path) != entry["sha256"]:
            raise ExportIntegrityError(f"dividend file does not match manifest: {key}")
        market, symbol = key.split("/", 1)
        with file_path.open(encoding="utf-8", newline="") as handle:
            out[(market, symbol)] = [
                (date.fromisoformat(r["date"]), float(r["amount"]))
                for r in csv.DictReader(handle)
            ]
    return out, sha256_file(path)
