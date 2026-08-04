"""Build synthetic KR corpus fixtures for harness wiring.

Fixture data lives under ``research/kr_corpus/backtest/fixtures/synthetic_v1/``
(or a caller-provided root). Dates are confined to the exploration window
(2023-01-01..2023-02-15 subset for compact smoke). **No holdout dates.**

OHLCV column names/types match the sealed kr-corpus-v1 schema (ticker/session/
value int64 + price_mode/source_product). Membership remains the harness
fixture contract (symbol/session_date/member/status).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from loader import sha256_bytes
from schema_contract import arrow_schema_for

__all__ = [
    "FIXTURE_REL_ROOT",
    "build_synthetic_fixture",
]

FIXTURE_REL_ROOT = Path(__file__).resolve().parent / "fixtures" / "synthetic_v1"

# Compact exploration-only calendar (weekdays). No 2025+ dates.
_FIXTURE_START = date(2023, 1, 2)
_FIXTURE_END = date(2023, 2, 15)
_SYMBOLS = (
    # (ticker, market, base_price_int, value_scale)
    ("005930", "KOSPI", 70_000, 1_000_000_000),
    ("000660", "KOSPI", 120_000, 800_000_000),
    ("035420", "KOSPI", 200_000, 500_000_000),
    ("247540", "KOSDAQ", 80_000, 300_000_000),
    ("086520", "KOSDAQ", 50_000, 200_000_000),
)


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_synthetic_fixture(root: Path | None = None) -> Path:
    """Write synthetic parquet shards + manifest under ``root``.

    Returns the fixture root path.
    """
    fixture_root = Path(root) if root is not None else FIXTURE_REL_ROOT
    fixture_root.mkdir(parents=True, exist_ok=True)

    sessions = _weekdays(_FIXTURE_START, _FIXTURE_END)
    ohlcv_rows: dict[tuple[str, int], list[dict[str, Any]]] = {}
    memb_rows: dict[tuple[str, int], list[dict[str, Any]]] = {}

    for i, session in enumerate(sessions):
        year = session.year
        for j, (ticker, market, base_px, value_scale) in enumerate(_SYMBOLS):
            # Simple deterministic walk; day index moves price (integer KRW).
            px = int(base_px + i * 10 + j * 100)
            trading_value = int(value_scale * (1 + ((i + j) % 7)) // 1)
            # Delist 086520 after mid window to exercise terminal path.
            delisted = ticker == "086520" and session >= date(2023, 2, 1)
            status = "delisted" if delisted else "listed"
            member = not delisted

            ohlcv_key = (market, year)
            ohlcv_rows.setdefault(ohlcv_key, []).append(
                {
                    "session": session.isoformat(),
                    "market": market,
                    "ticker": ticker,
                    "open": px,
                    "high": px + 100,
                    "low": px - 100,
                    "close": px,
                    "volume": 1_000_000 + 1000 * i,
                    "value": trading_value,
                    "price_mode": "adjusted",
                    "source_product": "synthetic_fixture",
                }
            )
            memb_rows.setdefault(ohlcv_key, []).append(
                {
                    "symbol": ticker,
                    "session_date": session.isoformat(),
                    "market": market,
                    "member": member,
                    "status": status,
                }
            )

    manifest: list[dict[str, Any]] = []
    ohlcv_schema = arrow_schema_for("ohlcv")
    memb_schema = arrow_schema_for("membership")

    for (market, year), rows in sorted(ohlcv_rows.items()):
        rel = f"ohlcv/{market}/{year}/bars.parquet"
        path = fixture_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=ohlcv_schema)
        pq.write_table(table, path)
        data = path.read_bytes()
        manifest.append(
            {
                "relative_path": rel,
                "file_sha256": sha256_bytes(data),
                "row_count": len(rows),
                "dataset": "ohlcv",
                "market": market,
                "year": year,
            }
        )

    for (market, year), rows in sorted(memb_rows.items()):
        rel = f"membership/{market}/{year}/members.parquet"
        path = fixture_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=memb_schema)
        pq.write_table(table, path)
        data = path.read_bytes()
        manifest.append(
            {
                "relative_path": rel,
                "file_sha256": sha256_bytes(data),
                "row_count": len(rows),
                "dataset": "membership",
                "market": market,
                "year": year,
            }
        )

    manifest_path = fixture_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    meta = {
        "fixture_id": "kr-backtest-harness-synthetic-v1",
        "JOB_PURPOSE": "BACKTEST_HARNESS_WIRING_ONLY",
        "label": "PIPELINE_SMOKE_NOT_A_STRATEGY",
        "schema_origin": "SEALED_CORPUS_V1",
        "window": {
            "start": _FIXTURE_START.isoformat(),
            "end": _FIXTURE_END.isoformat(),
        },
        "notes": [
            "Synthetic only — not real KR market data.",
            "Exploration window subset; no holdout dates.",
            "OHLCV columns match sealed kr-corpus-v1 (ticker/session/value int64).",
            "value is non-null here so value_rank wiring can exercise; sealed "
            "corpus may carry null value cells which must not be imputed.",
        ],
    }
    (fixture_root / "fixture_meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return fixture_root
