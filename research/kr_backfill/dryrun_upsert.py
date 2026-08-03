"""Stage A dry-run: prove the backfill upsert is non-destructive. ROLLBACK ONLY.

Validates three claims the brief requires before any Stage B write:

1. ``ON CONFLICT (time, symbol, venue) DO NOTHING`` leaves an existing row
   byte-identical even when the incoming row carries different values.
   (The production live-sync path uses DO UPDATE; the backfill must not.)
2. A genuinely new (symbol, time, venue) key inserts exactly one row.
3. No table other than ``kr_candles_1m`` changes.

Zero broker calls: the "incoming" rows are synthesised locally, so this runs
inside the market-hours freeze. The transaction is **always** rolled back via a
sentinel exception — there is no code path that commits.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

VENUE = "KRX"

BACKFILL_UPSERT_SQL = """
INSERT INTO public.kr_candles_1m
    (symbol, time, venue, open, high, low, close, volume, value)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (time, symbol, venue) DO NOTHING
"""

WITNESS_TABLES = (
    "public.kr_candles_1m",
    "public.kr_symbol_universe",
    "public.stock_info",
)


class RollbackSentinel(RuntimeError):
    """Raised solely to force rollback. Never indicates failure."""


def dsn() -> str:
    raw = os.environ["DATABASE_URL"]
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgresql://"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-csv", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    with args.symbols_csv.open() as fh:
        target_symbols = [r["ticker"] for r in csv.DictReader(fh)]

    result: dict = {
        "ran_at_utc": datetime.now(UTC).isoformat(),
        "committed": False,
        "broker_calls": 0,
        "checks": {},
    }

    conn = await asyncpg.connect(dsn())
    try:
        # --- pick fixtures (read-only, outside the write txn) ----------
        existing = await conn.fetchrow(
            "SELECT symbol, time, venue, open, high, low, close, volume, value "
            "FROM public.kr_candles_1m WHERE venue=$1 ORDER BY time DESC LIMIT 1",
            VENUE,
        )
        if existing is None:
            raise SystemExit("no existing KRX row to test against")

        present = {
            r["symbol"]
            for r in await conn.fetch(
                "SELECT DISTINCT symbol FROM public.kr_candles_1m WHERE symbol = ANY($1::text[])",
                target_symbols,
            )
        }
        absent = [s for s in target_symbols if s not in present]
        if not absent:
            raise SystemExit("no absent target symbol available for the insert test")
        new_symbol = absent[0]
        # Reuse an existing timestamp so the insert lands in an existing chunk:
        # no chunk DDL is created, so rollback has nothing to leave behind.
        new_time = existing["time"]

        result["fixtures"] = {
            "conflict_key": {
                "symbol": existing["symbol"],
                "time": existing["time"].isoformat(),
                "venue": existing["venue"],
            },
            "insert_key": {
                "symbol": new_symbol,
                "time": new_time.isoformat(),
                "venue": VENUE,
            },
        }

        before_counts = {
            t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in WITNESS_TABLES
        }
        before_row = dict(existing)

        # --- the write txn: always rolled back -------------------------
        try:
            async with conn.transaction():
                # (1) conflict path — deliberately different payload
                status = await conn.execute(
                    BACKFILL_UPSERT_SQL,
                    existing["symbol"],
                    existing["time"],
                    existing["venue"],
                    existing["open"] + 999,
                    existing["high"] + 999,
                    existing["low"] + 999,
                    existing["close"] + 999,
                    existing["volume"] + 999,
                    existing["value"] + 999,
                )
                conflict_inserted = int(status.split()[-1])

                after_row = dict(
                    await conn.fetchrow(
                        "SELECT symbol, time, venue, open, high, low, close, volume, value "
                        "FROM public.kr_candles_1m WHERE symbol=$1 AND time=$2 AND venue=$3",
                        existing["symbol"],
                        existing["time"],
                        existing["venue"],
                    )
                )

                # (2) genuine insert path
                status = await conn.execute(
                    BACKFILL_UPSERT_SQL,
                    new_symbol,
                    new_time,
                    VENUE,
                    1000,
                    1010,
                    990,
                    1005,
                    12345,
                    12345000,
                )
                new_inserted = int(status.split()[-1])

                in_txn_counts = {
                    t: await conn.fetchval(f"SELECT count(*) FROM {t}")
                    for t in WITNESS_TABLES
                }

                result["checks"]["conflict_rows_inserted"] = conflict_inserted
                result["checks"]["conflict_row_unchanged"] = after_row == before_row
                result["checks"]["new_key_rows_inserted"] = new_inserted
                result["checks"]["in_txn_counts"] = in_txn_counts

                raise RollbackSentinel
        except RollbackSentinel:
            result["rolled_back"] = True

        # --- post-rollback verification --------------------------------
        after_counts = {
            t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in WITNESS_TABLES
        }
        post_row = dict(
            await conn.fetchrow(
                "SELECT symbol, time, venue, open, high, low, close, volume, value "
                "FROM public.kr_candles_1m WHERE symbol=$1 AND time=$2 AND venue=$3",
                existing["symbol"],
                existing["time"],
                existing["venue"],
            )
        )

        result["checks"]["counts_before"] = before_counts
        result["checks"]["counts_after_rollback"] = after_counts
        result["checks"]["counts_restored"] = before_counts == after_counts
        result["checks"]["row_restored_after_rollback"] = post_row == before_row
        result["checks"]["other_tables_unchanged"] = all(
            before_counts[t] == after_counts[t]
            for t in WITNESS_TABLES
            if t != "public.kr_candles_1m"
        )

        c = result["checks"]
        result["verdict"] = (
            "PASS"
            if (
                c["conflict_rows_inserted"] == 0
                and c["conflict_row_unchanged"]
                and c["new_key_rows_inserted"] == 1
                and c["counts_restored"]
                and c["row_restored_after_rollback"]
            )
            else "FAIL"
        )
    finally:
        await conn.close()

    payload = json.dumps(result, indent=2, default=str, ensure_ascii=False) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    print(payload)


if __name__ == "__main__":
    asyncio.run(main())
