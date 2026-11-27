"""Read-only report: would the DART identity switch to (source, source_event_id) conflict? (hk 6408)

The DART collector now treats the receipt number as the event identity (one
``market_events`` row per ``source_event_id``, whatever its category). The unique
index that enforces that is NOT in this change. Before any migration swaps the
index, run this report against the database:

* ``multi_row_receipts`` — receipt numbers with more than one DART row. A DB
  index switch would fail on them; the collector would refuse to write them
  (``DartIdentityConflict``). They must be resolved by an operator, never by an
  automatic delete or merge.
* ``malformed_receipts`` — DART rows whose ``source_event_id`` is not a 14-digit
  receipt number (the OpenDART ``rcept_no`` shape). The identity scope is only
  sound if every DART ``source_event_id`` has that shape.

One ``READ ONLY`` transaction with an 8 s statement timeout, SELECT only. It
prints JSON with counts only: no receipt numbers, categories, ids, titles, names
or payloads. The database URL is read from the named environment variable and is
never printed. Exit 0
when there is no conflict and no malformed id, 1 when there is, 2 when the read
itself fails.

Usage::

    uv run python -m scripts.report_dart_identity_conflicts --database-url-env NAME
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SUMMARY_SQL = text(
    """
    SELECT
        count(*) AS dart_rows,
        count(DISTINCT source_event_id) AS receipts
    FROM market_events
    WHERE source = 'dart'
    """
)

# Counts only: how many receipt numbers have more than one row, and the largest
# number of rows any one receipt has. No receipt numbers, categories or ids.
CONFLICT_SQL = text(
    """
    SELECT
        count(*) AS multi_row_receipts,
        coalesce(max(row_count), 0) AS max_rows_per_receipt
    FROM (
        SELECT count(*) AS row_count
        FROM market_events
        WHERE source = 'dart' AND source_event_id IS NOT NULL
        GROUP BY source_event_id
        HAVING count(*) > 1
    ) AS per_receipt
    """
)

# A DART row with a NULL receipt number is malformed too: the identity scope
# needs every DART row to carry a 14-digit receipt number.
MALFORMED_SQL = text(
    """
    SELECT count(*) AS malformed_rows
    FROM market_events
    WHERE source = 'dart'
      AND (source_event_id IS NULL OR source_event_id !~ '^[0-9]{14}$')
    """
)


def build_report(
    *,
    summary: dict[str, Any],
    conflicts: dict[str, Any],
    malformed_rows: int,
) -> tuple[dict[str, Any], int]:
    """Pure: the counts-only JSON body and exit code for one set of read results."""
    multi_row = int(conflicts["multi_row_receipts"])
    clean = multi_row == 0 and malformed_rows == 0
    body = {
        "status": "clean" if clean else "conflicts",
        "dart_rows": int(summary["dart_rows"]),
        "receipts": int(summary["receipts"]),
        "multi_row_receipts": multi_row,
        "max_rows_per_receipt": int(conflicts["max_rows_per_receipt"]),
        "malformed_receipts": malformed_rows,
    }
    return body, 0 if clean else 1


async def _read(database_url: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SET LOCAL statement_timeout = '8s'"))
                summary = (await conn.execute(SUMMARY_SQL)).mappings().one()
                conflicts = (await conn.execute(CONFLICT_SQL)).mappings().one()
                malformed = (await conn.execute(MALFORMED_SQL)).scalar_one()
        return dict(summary), dict(conflicts), int(malformed)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url-env",
        required=True,
        help="Name of the environment variable holding the database URL "
        "(the value is never printed).",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        print(
            json.dumps({"status": "missing_database_url", "env": args.database_url_env})
        )
        return 2
    try:
        summary, conflicts, malformed = asyncio.run(_read(database_url))
    except Exception as exc:  # noqa: BLE001 - a failed read must be visible, not silent
        print(json.dumps({"status": "unavailable", "error_class": type(exc).__name__}))
        return 2
    body, code = build_report(
        summary=summary, conflicts=conflicts, malformed_rows=malformed
    )
    print(json.dumps(body, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
