#!/usr/bin/env python3
"""Refill symbols of KR DART rows stored without one (hk 5623).

Examples:
    # Preview: re-collect the days, resolve, count per reason, write nothing
    python -m scripts.backfill_dart_symbols --from-date 2026-10-01 --to-date 2026-10-31

    # Commit: write only the symbol column of rows that now resolve
    python -m scripts.backfill_dart_symbols --from-date 2026-10-01 --to-date 2026-10-31 --commit

Preview is the default. ``--commit`` is the only write switch. There is no
scheduler registration. ``first_received_at``, ``raw_payload_json``, and every
other stored value are left unchanged. ``updated_at`` is deliberately NOT moved
when a symbol is filled: the daily-plan bundle treats ``updated_at > created_at``
as ``observed_time_unproven`` (design 5750 section 7.1), so bumping it would
downgrade these rows for no change in value. Rows that still do not resolve stay
NULL; their per-reason counts in the JSON output are the audit trail. Exit code 0
when every affected day was fetched; 1 when a day's DART fetch failed (the report
lists each one). Unresolved rows are not failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.market_events.dart_helpers import (
    fetch_dart_corp_registry,
    fetch_dart_filings_for_date,
)
from app.services.market_events.dart_symbol_backfill import (
    RepositoryDartSymbolStore,
    backfill_dart_symbols,
)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refill symbols of KR DART rows stored without one (hk 5623)."
    )
    parser.add_argument("--from-date", type=_parse_iso, dest="from_date", required=True)
    parser.add_argument("--to-date", type=_parse_iso, dest="to_date", required=True)
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Write the resolved symbol column only. Without this flag nothing is "
            "written. updated_at, first_received_at, and raw_payload_json are never "
            "changed (updated_at > created_at would mark the row observed_time_unproven "
            "in the daily-plan bundle). Unresolved rows stay NULL; per-reason counts "
            "in the JSON output are the audit."
        ),
    )
    ns = parser.parse_args(argv)
    if ns.from_date > ns.to_date:
        parser.error("--from-date must be less than or equal to --to-date")
    return ns


async def _fetch_day(target_date: date) -> list[dict]:
    # Backfill-style: every attempt is a real OpenDART call, never a cached page.
    return await fetch_dart_filings_for_date(target_date)


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="dart-symbol-backfill")
    ns = parse_args(argv)

    async with AsyncSessionLocal() as db:
        report = await backfill_dart_symbols(
            RepositoryDartSymbolStore(db),
            from_date=ns.from_date,
            to_date=ns.to_date,
            commit=ns.commit,
            fetch_rows=_fetch_day,
            load_corp_registry=fetch_dart_corp_registry,
        )

    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
