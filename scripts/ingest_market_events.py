#!/usr/bin/env python3
"""Per-day market events ingestion CLI (ROB-128).

Examples:
    python -m scripts.ingest_market_events \\
        --source finnhub --category earnings --market us \\
        --from-date 2026-05-07 --to-date 2026-05-14

    python -m scripts.ingest_market_events \\
        --source dart --category disclosure --market kr \\
        --from-date 2026-05-07 --to-date 2026-05-07

The command splits the [from_date, to_date] range into single-day partitions and
invokes the ingestion orchestrator per day. Failures are recorded as failed
partitions; subsequent runs only retry failed days when re-invoked with the same
range.

Recommended rolling window for later Prefect schedule:
    today - 7 days through today + 60 days
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterator
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.monitoring.sentry import capture_exception, init_sentry
from app.services.market_events.ingestion import (
    ingest_economic_events_for_date,
    ingest_kr_disclosures_for_date,
    ingest_us_earnings_for_date,
)

logger = logging.getLogger(__name__)


SUPPORTED = {
    ("finnhub", "earnings", "us"): ingest_us_earnings_for_date,
    ("dart", "disclosure", "kr"): ingest_kr_disclosures_for_date,
    ("forexfactory", "economic", "global"): ingest_economic_events_for_date,
}


def iter_partition_dates(from_date: date, to_date: date) -> Iterator[date]:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Per-day market events ingestion CLI (ROB-128)."
    )
    parser.add_argument(
        "--source",
        default="finnhub",
        choices=["finnhub", "dart", "forexfactory"],
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
    parser.add_argument("--from-date", required=True, type=_parse_iso, dest="from_date")
    parser.add_argument("--to-date", required=True, type=_parse_iso, dest="to_date")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    ns = parser.parse_args(argv)

    key = (ns.source, ns.category, ns.market)
    if key not in SUPPORTED:
        parser.error(
            f"unsupported source/category/market combination: {key}. "
            f"supported: {sorted(SUPPORTED.keys())}"
        )
    return ns


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
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_sentry(service_name="market-events-ingest")
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
