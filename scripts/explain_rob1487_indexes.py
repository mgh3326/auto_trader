#!/usr/bin/env python3
"""ROB-1487: read-only EXPLAIN for the two ``max()`` partition probes.

Prints, for each hot query, the current plan ("after") and the plan the planner
falls back to with index scans disabled ("before" — the same shape it had when
neither query was index-supported). Nothing here mutates the database: no DDL,
no DML, no lock beyond the EXPLAIN itself.

Usage::

    uv run python -m scripts.explain_rob1487_indexes --database-url-env DATABASE_URL

The URL value is never printed; a missing variable is reported by name only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

_MOMENTUM_QUERY = (
    "SELECT max(snapshot_at) FROM invest_momentum_event_snapshots WHERE market = $1"
)
_SCREENER_QUERY = (
    "SELECT max(snapshot_date) FROM invest_screener_snapshots "
    "WHERE market = $1 AND support_computed_at IS NOT NULL"
)

_SIZES_SQL = """
SELECT relname, reltuples::bigint AS estimated_rows,
       pg_size_pretty(pg_total_relation_size(oid)) AS total_size
FROM pg_class
WHERE relname IN ('invest_momentum_event_snapshots', 'invest_screener_snapshots')
ORDER BY relname
"""


async def _explain(
    conn: asyncpg.Connection, sql: str, *, allow_index: bool
) -> list[str]:
    async with conn.transaction():
        if not allow_index:
            await conn.execute("SET LOCAL enable_indexscan = off")
            await conn.execute("SET LOCAL enable_bitmapscan = off")
            await conn.execute("SET LOCAL enable_indexonlyscan = off")
        rows = await conn.fetch(f"EXPLAIN (COSTS OFF) {sql}", "kr")
    return [row[0] for row in rows]


async def _run(database_url: str) -> int:
    conn = await asyncpg.connect(database_url, timeout=15)
    try:
        print("== table sizes (this database) ==")
        for row in await conn.fetch(_SIZES_SQL):
            print(
                f"{row['relname']}: ~{row['estimated_rows']} rows, {row['total_size']}"
            )

        for label, sql in (
            ("max(snapshot_at) / invest_momentum_event_snapshots", _MOMENTUM_QUERY),
            (
                "max(snapshot_date) / invest_screener_snapshots (support only)",
                _SCREENER_QUERY,
            ),
        ):
            print(f"\n== {label} ==")
            print("-- before (planner without index scans)")
            for line in await _explain(conn, sql, allow_index=False):
                print(f"   {line}")
            print("-- after")
            for line in await _explain(conn, sql, allow_index=True):
                print(f"   {line}")
    finally:
        await conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-env",
        default="DATABASE_URL",
        help="environment variable holding the PostgreSQL DSN (never printed)",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.database_url_env)
    if not dsn:
        print(
            f"missing environment variable {args.database_url_env!r}", file=sys.stderr
        )
        return 2
    return asyncio.run(_run(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
