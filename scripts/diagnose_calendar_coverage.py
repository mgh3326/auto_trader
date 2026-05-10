#!/usr/bin/env python3
"""Read-only calendar coverage / freshness diagnostic CLI (ROB-167).

Prints a per-source freshness summary and per-day partition matrix for
[from_date, to_date]. NEVER writes to the database; safe to run against
production.

Examples:
    uv run python -m scripts.diagnose_calendar_coverage \
        --from-date 2026-05-11 --to-date 2026-05-17

    uv run python -m scripts.diagnose_calendar_coverage \
        --from-date 2026-05-11 --to-date 2026-05-17 --json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, date, datetime

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.services.market_events.freshness_service import (
    MarketEventsFreshnessService,
)

logger = logging.getLogger(__name__)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only calendar coverage diagnostic CLI (ROB-167)."
    )
    parser.add_argument("--from-date", required=True, type=_parse_iso, dest="from_date")
    parser.add_argument("--to-date", required=True, type=_parse_iso, dest="to_date")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%MZ")


def _print_human(matrix) -> None:
    print(
        f"\nCalendar coverage: {matrix.fromDate}..{matrix.toDate} "
        f"(asOf {_fmt_dt(matrix.asOf)})"
    )
    print(
        f"  expected={matrix.coverage.expectedPartitions} "
        f"succeeded={matrix.coverage.succeededPartitions} "
        f"failed={matrix.coverage.failedPartitions} "
        f"missing={matrix.coverage.missingPartitions} "
        f"events={matrix.coverage.totalEvents}\n"
    )
    print("Source freshness:")
    print(
        f"  {'source':<14} {'category':<11} {'mkt':<7} {'state':<8} "
        f"{'succ':>5} {'fail':>5} {'miss':>5} {'events':>7} last_success"
    )
    for s in matrix.sources:
        print(
            f"  {s.source:<14} {s.category:<11} {s.market:<7} {s.state:<8} "
            f"{s.succeededPartitions:>5} {s.failedPartitions:>5} "
            f"{s.missingPartitions:>5} {s.eventCount:>7} "
            f"{_fmt_dt(s.lastSuccessAt)}"
        )
        if s.lastError:
            print(f"      lastError: {s.lastError}")
    print("\nPartitions:")
    print(
        f"  {'date':<11} {'source':<14} {'category':<11} {'mkt':<7} "
        f"{'status':<18} {'events':>6} finished_at"
    )
    for p in matrix.partitions:
        print(
            f"  {p.partitionDate.isoformat():<11} {p.source:<14} "
            f"{p.category:<11} {p.market:<7} {p.status:<18} "
            f"{p.eventCount:>6} {_fmt_dt(p.finishedAt)}"
        )


async def run(*, from_date: date, to_date: date, as_json: bool) -> int:
    async with AsyncSessionLocal() as db:
        svc = MarketEventsFreshnessService(db)
        matrix = await svc.get_coverage_matrix(from_date, to_date)
    if as_json:
        print(matrix.model_dump_json())
    else:
        _print_human(matrix)
    return 0


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="diagnose-calendar-coverage")
    ns = parse_args(argv)
    try:
        return await run(from_date=ns.from_date, to_date=ns.to_date, as_json=ns.as_json)
    except Exception as exc:
        logger.exception("diagnose_calendar_coverage crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
