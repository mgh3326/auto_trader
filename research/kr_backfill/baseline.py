"""Stage A baseline capture for the KR 1m backfill. READ-ONLY.

Opens the session with ``default_transaction_read_only = on`` so a stray write
fails loudly rather than silently landing in production. Captures the "before"
picture needed to make Stage D's before/after comparison meaningful:

* existing kr_candles_1m rows for the target 500 x target 1-year window
* per-symbol and per-month coverage
* continuous-aggregate watermarks
* live query latency samples for the recent-window read path
* retention-policy state (the constraint that governs what can survive)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import asyncpg

KST = timezone(timedelta(hours=9))

#: Target backfill window: trailing 1 year, KST session boundaries.
WINDOW_END_KST = datetime(2026, 8, 4, 0, 0, tzinfo=KST)
WINDOW_START_KST = datetime(2025, 8, 4, 0, 0, tzinfo=KST)

VENUE = "KRX"


def dsn() -> str:
    raw = os.environ["DATABASE_URL"]
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgresql://"
    )


def load_symbols(csv_path: Path) -> list[str]:
    with csv_path.open() as fh:
        return [r["ticker"] for r in csv.DictReader(fh)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-csv", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--latency-reps", type=int, default=15)
    args = ap.parse_args()

    symbols = load_symbols(args.symbols_csv)
    out: dict = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "window_start_kst": WINDOW_START_KST.isoformat(),
        "window_end_kst": WINDOW_END_KST.isoformat(),
        "venue": VENUE,
        "target_symbol_count": len(symbols),
        "read_only_session": True,
    }

    conn = await asyncpg.connect(dsn())
    try:
        await conn.execute("SET default_transaction_read_only = on")

        # -- whole-table shape ------------------------------------------
        row = await conn.fetchrow(
            "SELECT count(*) AS rows, min(time) AS min_t, max(time) AS max_t, "
            "count(DISTINCT symbol) AS symbols FROM public.kr_candles_1m"
        )
        out["table_total"] = {
            "rows": row["rows"],
            "min_time": row["min_t"].isoformat() if row["min_t"] else None,
            "max_time": row["max_t"].isoformat() if row["max_t"] else None,
            "distinct_symbols": row["symbols"],
        }

        # -- retention policy (the governing constraint) -----------------
        rows = await conn.fetch(
            """
            SELECT hypertable_name, config, scheduled, next_start
            FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_retention'
              AND hypertable_name LIKE 'kr_candles%'
            ORDER BY hypertable_name
            """
        )
        out["retention_policies"] = [
            {
                "hypertable": r["hypertable_name"],
                "config": json.loads(r["config"]) if r["config"] else None,
                "scheduled": r["scheduled"],
                "next_start": r["next_start"].isoformat() if r["next_start"] else None,
            }
            for r in rows
        ]

        # -- target scope: existing rows in the 500 x 1y window ----------
        row = await conn.fetchrow(
            """
            SELECT count(*) AS rows, count(DISTINCT symbol) AS symbols
            FROM public.kr_candles_1m
            WHERE symbol = ANY($1::text[])
              AND venue = $2
              AND time >= $3 AND time < $4
            """,
            symbols,
            VENUE,
            WINDOW_START_KST,
            WINDOW_END_KST,
        )
        out["target_window_existing"] = {
            "rows": row["rows"],
            "distinct_symbols_present": row["symbols"],
        }

        # -- per-month coverage inside the target window -----------------
        rows = await conn.fetch(
            """
            SELECT to_char(date_trunc('month', time AT TIME ZONE 'Asia/Seoul'), 'YYYY-MM') AS ym,
                   count(*) AS rows, count(DISTINCT symbol) AS symbols
            FROM public.kr_candles_1m
            WHERE symbol = ANY($1::text[])
              AND venue = $2
              AND time >= $3 AND time < $4
            GROUP BY 1 ORDER BY 1
            """,
            symbols,
            VENUE,
            WINDOW_START_KST,
            WINDOW_END_KST,
        )
        out["target_window_by_month"] = [
            {"month_kst": r["ym"], "rows": r["rows"], "symbols": r["symbols"]}
            for r in rows
        ]

        # -- per-symbol counts (only those present) ----------------------
        rows = await conn.fetch(
            """
            SELECT symbol, count(*) AS rows, min(time) AS min_t, max(time) AS max_t
            FROM public.kr_candles_1m
            WHERE symbol = ANY($1::text[])
              AND venue = $2
              AND time >= $3 AND time < $4
            GROUP BY symbol ORDER BY symbol
            """,
            symbols,
            VENUE,
            WINDOW_START_KST,
            WINDOW_END_KST,
        )
        out["target_window_by_symbol"] = [
            {
                "symbol": r["symbol"],
                "rows": r["rows"],
                "min_time": r["min_t"].isoformat(),
                "max_time": r["max_t"].isoformat(),
            }
            for r in rows
        ]

        # -- cagg watermarks --------------------------------------------
        caggs = []
        for view in (
            "kr_candles_5m",
            "kr_candles_15m",
            "kr_candles_30m",
            "kr_candles_1h",
        ):
            r = await conn.fetchrow(
                f"SELECT count(*) AS rows, min(bucket) AS min_b, max(bucket) AS max_b "
                f"FROM public.{view}"
            )
            caggs.append(
                {
                    "view": view,
                    "rows": r["rows"],
                    "min_bucket": r["min_b"].isoformat() if r["min_b"] else None,
                    "max_bucket": r["max_b"].isoformat() if r["max_b"] else None,
                }
            )
        out["continuous_aggregates"] = caggs

        # -- live query latency baseline (recent-window read path) -------
        probe_symbols = symbols[:5]
        latency: dict[str, dict] = {}

        async def timed(label: str, sql: str, *params) -> None:
            samples = []
            for _ in range(args.latency_reps):
                t0 = time.perf_counter()
                await conn.fetch(sql, *params)
                samples.append((time.perf_counter() - t0) * 1000.0)
            latency[label] = {
                "reps": len(samples),
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[int(len(samples) * 0.95) - 1], 3),
                "min_ms": round(min(samples), 3),
                "max_ms": round(max(samples), 3),
            }

        now = datetime.now(UTC)
        for sym in probe_symbols:
            await timed(
                f"recent_1d:{sym}",
                "SELECT time, open, high, low, close, volume, value FROM public.kr_candles_1m "
                "WHERE symbol=$1 AND time >= $2 ORDER BY time DESC LIMIT 500",
                sym,
                now - timedelta(days=1),
            )
        for sym in probe_symbols[:3]:
            await timed(
                f"recent_1w_5m:{sym}",
                "SELECT bucket, open, high, low, close, volume FROM public.kr_candles_5m "
                "WHERE symbol=$1 AND bucket >= $2 ORDER BY bucket DESC LIMIT 500",
                sym,
                now - timedelta(days=7),
            )
        out["query_latency_baseline"] = latency

    finally:
        await conn.close()

    payload = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    print(f"baseline written: {args.out}")
    print(f"baseline sha256: {hashlib.sha256(payload.encode()).hexdigest()}")
    print(
        json.dumps(
            {k: v for k, v in out.items() if k != "target_window_by_symbol"},
            indent=2,
            ensure_ascii=False,
        )[:3000]
    )


if __name__ == "__main__":
    asyncio.run(main())
