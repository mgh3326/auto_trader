#!/usr/bin/env python3
"""Per-day market events ingestion CLI (ROB-128, ROB-132, ROB-171).

Examples:
    # US earnings, explicit range
    python -m scripts.ingest_market_events \\
        --source finnhub --category earnings --market us \\
        --from-date 2026-05-07 --to-date 2026-05-14

    # KR DART disclosures, single day
    python -m scripts.ingest_market_events \\
        --source dart --category disclosure --market kr \\
        --from-date 2026-05-07 --to-date 2026-05-07

    # KR earnings via WiseFn, whole month (ROB-171)
    python -m scripts.ingest_market_events \\
        --source wisefn --category earnings --market kr \\
        --month 2026-05 --dry-run

`--month YYYY-MM` is a thin wrapper that expands to
`--from-date <first day of month> --to-date <last day of month>` and is
mutually exclusive with `--from-date/--to-date`. The pipeline still loops
per-day partitions internally — the `MarketEventIngestionPartition` shape is
unchanged.

Operational gates:
* `wisefn` is only invoked when `settings.wisefn_earnings_enabled` is True.
  Otherwise the CLI logs a warning and exits 0 without DB writes.

Recommended rolling window for later Prefect schedule:
    today - 7 days through today + 60 days
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import logging
import re
from collections.abc import Iterator
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_kr_earnings_wisefn_for_date,
    ingest_us_earnings_for_date,
)

logger = logging.getLogger(__name__)


SUPPORTED = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
    ("forexfactory", "economic", "global"): ingest_economic_events_for_date,
    ("wisefn", "earnings", "kr"): ingest_kr_earnings_wisefn_for_date,
}


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def iter_partition_dates(from_date: date, to_date: date) -> Iterator[date]:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


def month_to_date_range(month: str) -> tuple[date, date]:
    """Expand 'YYYY-MM' to (first_day, last_day) inclusive."""
    m = _MONTH_RE.match(month)
    if not m:
        raise ValueError(f"--month must be YYYY-MM, got {month!r}")
    year = int(m.group(1))
    mo = int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"--month month component out of range: {month!r}")
    last_day = calendar.monthrange(year, mo)[1]
    return date(year, mo, 1), date(year, mo, last_day)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Per-day market events ingestion CLI (ROB-128 / 132 / 171)."
    )
    parser.add_argument(
        "--source",
        default="finnhub",
        choices=["finnhub", "dart", "forexfactory", "wisefn"],
    )
    parser.add_argument(
        "--category",
        default="earnings",
        choices=["earnings", "disclosure", "economic"],
    )
    parser.add_argument(
        "--market",
        default="us",
        choices=["us", "kr", "global"],
    )

    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument(
        "--month",
        type=str,
        default=None,
        help="Whole-month batch as YYYY-MM. Expands to first..last day of month.",
    )
    range_group.add_argument(
        "--from-date",
        type=_parse_iso,
        dest="from_date",
        help="ISO start date (inclusive). Requires --to-date.",
    )

    parser.add_argument(
        "--to-date",
        type=_parse_iso,
        dest="to_date",
        help="ISO end date (inclusive). Required when --from-date is used.",
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    ns = parser.parse_args(argv)

    if ns.month is not None:
        if ns.to_date is not None:
            parser.error("--month is mutually exclusive with --to-date")
        try:
            ns.from_date, ns.to_date = month_to_date_range(ns.month)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if ns.to_date is None:
            parser.error("--to-date is required when --from-date is used")

    key = (ns.source, ns.category, ns.market)
    if key not in SUPPORTED:
        parser.error(
            f"unsupported source/category/market combination: {key}. "
            f"supported: {sorted(SUPPORTED.keys())}"
        )
    return ns


def _is_source_enabled(
    source: str, category: str, market: str
) -> tuple[bool, str | None]:
    """Return (enabled, reason_when_disabled) for a configured source."""
    if (source, category, market) == ("wisefn", "earnings", "kr"):
        if not settings.wisefn_earnings_enabled:
            return False, (
                "wisefn earnings ingestion disabled "
                "(set WISEFN_EARNINGS_ENABLED=true to enable)"
            )
    return True, None


async def run_ingest(
    *,
    db: AsyncSession,
    source: str,
    category: str,
    market: str,
    from_date: date,
    to_date: date,
    dry_run: bool,
) -> int:
    enabled, reason = _is_source_enabled(source, category, market)
    if not enabled and not dry_run:
        logger.warning("%s; skipping run for %s..%s", reason, from_date, to_date)
        return 0

    fn = SUPPORTED[(source, category, market)]
    succeeded = 0
    failed = 0
    for d in iter_partition_dates(from_date, to_date):
        if dry_run:
            logger.info(
                "[DRY-RUN] would ingest %s/%s/%s for %s", source, category, market, d
            )
            succeeded += 1
            continue
        result = await fn(db, d)
        await db.commit()
        if result.status == "succeeded":
            succeeded += 1
            logger.info(
                "ingested %s events for %s/%s/%s on %s",
                result.event_count,
                source,
                category,
                market,
                d,
            )
        else:
            failed += 1
            logger.error(
                "ingest failed for %s/%s/%s on %s: %s",
                source,
                category,
                market,
                d,
                result.error,
            )
    summary = {
        "source": source,
        "category": category,
        "market": market,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dry_run": dry_run,
        "succeeded": succeeded,
        "failed": failed,
    }
    import json as _json

    print(_json.dumps(summary))
    logger.info("ingest complete: %s", summary)
    return 0 if failed == 0 else 2


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="market-events-ingest")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_ingest(
                db=db,
                source=ns.source,
                category=ns.category,
                market=ns.market,
                from_date=ns.from_date,
                to_date=ns.to_date,
                dry_run=ns.dry_run,
            )
    except Exception as exc:
        capture_exception(exc, process="ingest_market_events")
        logger.error("ingest_market_events crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
