"""A tiny export and price snapshot written to disk, using the real export columns."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from app.services.dots_history_export.spec import SCHEMA_VERSION, TABLE_BY_NAME

TABLES = (
    "kis_live_order_ledger",
    "live_order_ledger",
    "toss_live_order_ledger",
    "execution_ledger",
    "order_proposals",
    "order_proposal_rungs",
)
START = date(2026, 5, 1)


def weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_export(root: Path, rows: dict[str, list[dict]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tables = {}
    for name in TABLES:
        columns = list(TABLE_BY_NAME[name].column_names)
        path = root / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, restval="")
            writer.writeheader()
            writer.writerows(rows.get(name, []))
        tables[name] = {
            "columns": columns,
            "row_count": len(rows.get(name, [])),
            "files": {
                "csv": {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": _digest(path),
                }
            },
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "window": {
            "since_utc": "2026-05-01T00:00:00+00:00",
            "until_utc": "2026-11-01T00:00:00+00:00",
        },
        "tables": tables,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def write_prices(root: Path, series: dict[str, list[tuple[date, float]]]) -> Path:
    """``series`` keys are ``market/symbol`` or ``benchmark/market``."""
    entries = {}
    for key, bars in series.items():
        path = root / f"{key}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "close", "split"])
            for day, close in bars:
                writer.writerow([day.isoformat(), close, close, 0.0])
        entries[key] = {
            "file": f"{key}.csv",
            "source": "fixture",
            "rows": len(bars),
            "first": bars[0][0].isoformat(),
            "last": bars[-1][0].isoformat(),
            "sha256": _digest(path),
        }
    manifest = {
        "schema": "trade-retro-prices/v1",
        "fetched_at_utc": "2026-11-01T00:00:00+00:00",
        "start": START.isoformat(),
        "end_exclusive": "2026-11-01",
        "entries": entries,
        "missing": {},
    }
    (root / "prices_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def fill_row(
    alias,
    market,
    symbol,
    side,
    qty,
    price,
    stamp,
    ref,
    day="",
    source="reconciler",
    created=None,
):
    return {
        "row_ref": f"row_{ref}{stamp}",
        "account_alias": alias,
        "market": market,
        "symbol": symbol,
        "side": side,
        "filled_qty": str(qty),
        "fill_price": str(price),
        "currency": "USD" if market == "us" else "KRW",
        "fill_source": source,
        "superseded_duplicate": "false",
        "order_ref": ref,
        "trading_day": day,
        "filled_at_utc": stamp,
        "created_at_utc": created or stamp,
    }


def order_row(
    alias, market, symbol, side, qty, limit, stamp, ref, outcome, day="", **extra
):
    row = {
        "row_ref": f"row_{ref}",
        "account_alias": alias,
        "market": market,
        "symbol": symbol,
        "side": side,
        "order_type": "limit",
        "qty": str(qty),
        "limit_price": str(limit),
        "terminal_outcome": outcome,
        "order_ref": ref,
        "trading_day": day,
        "created_at_utc": stamp,
        "accepted_at_utc": stamp,
    }
    row.update(extra)
    return row


#: Symbols and aliases a public report must never show.
PRIVATE_SYMBOLS = ("AAA", "005930", "XYZ")
PRIVATE_ALIASES = ("kis_live", "toss_live", "upbit_live")


def sample_export(root: Path) -> Path:
    """One trade per market, each with a distinctive size to catch a leaked amount."""
    rows = {
        "execution_ledger": [
            fill_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                7,
                100,
                "2026-05-04T14:00:00Z",
                "ordus1",
            ),
            fill_row(
                "toss_live",
                "us",
                "AAA",
                "sell",
                3,
                120,
                "2026-05-11T14:00:00Z",
                "ordus2",
            ),
            fill_row(
                "kis_live",
                "kr",
                "005930",
                "buy",
                11,
                50000,
                "2026-05-04T01:00:00Z",
                "ordkr1",
                day="2026-05-04",
            ),
            fill_row(
                "upbit_live",
                "crypto",
                "XYZ",
                "buy",
                13,
                2000,
                "2026-05-04T01:00:00Z",
                "ordc1",
            ),
            fill_row(
                "kis_live",
                "kr",
                "000270",
                "buy",
                5,
                40000,
                "2026-05-10T00:00:00Z",
                "ordseed",
                day="2026-05-10",
                source="manual_import",
            ),
        ],
        "toss_live_order_ledger": [
            order_row(
                "toss_live",
                "us",
                "AAA",
                "buy",
                7,
                100,
                "2026-05-04T13:50:00Z",
                "ordus1",
                "open",
            ),
            order_row(
                "toss_live",
                "us",
                "AAA",
                "sell",
                3,
                120,
                "2026-05-11T13:50:00Z",
                "ordus2",
                "open",
            ),
        ],
        "kis_live_order_ledger": [
            order_row(
                "kis_live",
                "kr",
                "005930",
                "buy",
                11,
                50000,
                "2026-05-04T00:50:00Z",
                "ordkr1",
                "filled",
                day="2026-05-04",
                filled_qty="11",
                fill_price="50000",
            ),
            order_row(
                "kis_live",
                "kr",
                "005930",
                "buy",
                9,
                40000,
                "2026-05-05T00:50:00Z",
                "ordkr2",
                "expired",
                day="2026-05-05",
            ),
        ],
        "live_order_ledger": [
            order_row(
                "upbit_live",
                "crypto",
                "KRW-XYZ",
                "buy",
                13,
                2000,
                "2026-05-04T00:50:00Z",
                "ordc1",
                "filled",
                filled_qty="13",
                fill_price="2000",
            ),
        ],
    }
    return write_export(root, rows)


def sample_prices(root: Path) -> Path:
    days = weekdays(START, 40)
    ramp = lambda base, step: [(d, base + step * i) for i, d in enumerate(days)]  # noqa: E731
    return write_prices(
        root,
        {
            "benchmark/kr": ramp(1000.0, 1.0),
            "benchmark/us": ramp(5000.0, 5.0),
            "benchmark/crypto": ramp(90000.0, 10.0),
            "us/AAA": ramp(100.0, 1.0),
            "kr/005930": ramp(50000.0, 100.0),
            "kr/000270": ramp(41000.0, 50.0),
            "crypto/XYZ": ramp(2000.0, 5.0),
        },
    )
