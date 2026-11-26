#!/usr/bin/env python3
"""Benzinga calendar ingestion CLI (hk 6513).

Manual lever only — nothing registers a schedule. Dry-run is the default; the
``--commit`` flag is what writes to ``market_events``.

Examples:
    # Preview the premarket set for the next week (no network, no writes)
    python -m scripts.ingest_benzinga_calendars \\
        --category earnings,analyst_rating,guidance,dividend,economic \\
        --from-date 2026-11-25 --to-date 2026-12-02

    # Write ratings for today
    python -m scripts.ingest_benzinga_calendars \\
        --category analyst_rating --date 2026-11-25 --commit

    # Short interest needs an explicit symbol list (the endpoint requires it)
    python -m scripts.ingest_benzinga_calendars \\
        --category short_interest --date 2026-11-25 \\
        --symbols NVDA,AMD --commit

    # Replacement verdict: stored Benzinga vs Finnhub earnings, read-only
    python -m scripts.ingest_benzinga_calendars \\
        --compare-earnings --from-date 2026-10-01 --to-date 2026-11-25

Gates: ``BENZINGA_CALENDARS_ENABLED`` must be exactly ``"true"`` and
``BENZINGA_API_KEY`` must hold the key. The gate is checked before anything
else, for every mode including ``--compare-earnings`` — with it off the command
makes no network call, opens no session and exits 2. Missing key reports the
env variable name only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.core.timezone import today_kst
from app.monitoring.sentry import capture_exception
from app.services.benzinga_calendars.client import BenzingaCalendarClient
from app.services.benzinga_calendars.compare import compare_earnings
from app.services.benzinga_calendars.ingestion import (
    dates_in_range,
    ingest_benzinga_category_for_range,
)
from app.services.benzinga_calendars.spec import CATEGORIES, endpoint_for
from app.services.benzinga_http import (
    BenzingaApiKeyMissing,
    calendars_enabled,
    read_api_key,
)
from app.services.market_events.query_service import MarketEventsQueryService

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 2
GATE_OFF = 2


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def _split_categories(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token and token not in out:
                out.append(token)
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest Benzinga calendars into market_events (hk 6513)."
    )
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help=f"Comma-separated or repeated. Supported: {', '.join(CATEGORIES)}",
    )
    range_group = parser.add_mutually_exclusive_group()
    range_group.add_argument(
        "--date", type=_parse_iso, default=None, help="Single ISO date."
    )
    range_group.add_argument(
        "--from-date", type=_parse_iso, dest="from_date", default=None
    )
    parser.add_argument("--to-date", type=_parse_iso, dest="to_date", default=None)
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated tickers (required for short_interest, optional "
        "as a filter elsewhere).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write to the database. Without it the run only previews.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reprocess partitions already marked succeeded. Required to capture "
            "provider revisions: without it a (category, date) partition is "
            "fetched once and never again, so the shipped DAGs pass --force for "
            "the dates they exist to refresh. Same-payload re-reads still write "
            "nothing."
        ),
    )
    parser.add_argument("--max-pages", type=int, default=20, dest="max_pages")
    parser.add_argument(
        "--compare-earnings",
        action="store_true",
        dest="compare_earnings",
        help="Read-only Benzinga vs Finnhub earnings comparison for the window.",
    )
    ns = parser.parse_args(argv)

    ns.dry_run = not ns.commit
    ns.categories = _split_categories(ns.category or [])

    if ns.date is not None:
        if ns.to_date is not None or ns.from_date is not None:
            parser.error("--date is mutually exclusive with --from-date/--to-date")
        ns.from_date = ns.date
        ns.to_date = ns.date
    elif ns.from_date is None or ns.to_date is None:
        today = today_kst()
        ns.from_date = ns.from_date or today
        ns.to_date = ns.to_date or (ns.from_date + timedelta(days=7))

    if ns.from_date > ns.to_date:
        parser.error("--from-date must be <= --to-date")

    if ns.compare_earnings:
        return ns

    if not ns.categories:
        parser.error("--category is required")
    unknown = [c for c in ns.categories if c not in CATEGORIES]
    if unknown:
        parser.error(f"unknown --category {unknown}; supported: {list(CATEGORIES)}")
    missing_symbols = {"short_interest"}.intersection(ns.categories)
    if missing_symbols and not ns.symbols:
        parser.error(
            f"--symbols is required for categories: {', '.join(sorted(missing_symbols))}"
        )
    return ns


async def _run_compare(from_date: date, to_date: date, symbols: str | None) -> int:
    """Read-only coverage comparison over stored earnings rows."""
    from app.core.symbol import to_db_symbol

    requested = {
        to_db_symbol(token.strip()).upper()
        for token in (symbols or "").split(",")
        if token.strip()
    }

    async with AsyncSessionLocal() as db:
        svc = MarketEventsQueryService(db)
        finnhub = await svc.list_for_range(
            from_date,
            to_date,
            category="earnings",
            market="us",
            source="finnhub",
        )
        benzinga = await svc.list_for_range(
            from_date,
            to_date,
            category="earnings",
            market="us",
            source="benzinga",
        )

    def _rows(events: list[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in events:
            symbol = to_db_symbol(event.symbol).upper() if event.symbol else None
            # Every requested symbol narrows both collections (hk 6513 r6): a
            # multi-symbol ``--symbols`` must not compare unrelated rows.
            if requested and symbol not in requested:
                continue
            rows.append(
                {
                    "symbol": event.symbol,
                    "date": event.event_date.isoformat(),
                    "time_hint": event.time_hint,
                }
            )
        return rows

    report = compare_earnings(_rows(finnhub.events), _rows(benzinga.events))
    report["from_date"] = from_date.isoformat()
    report["to_date"] = to_date.isoformat()
    print(json.dumps({"compare_earnings": report}))
    return EXIT_OK


async def run(ns: argparse.Namespace) -> int:
    # The gate covers every mode, including the read-only comparison: with it
    # off the command must not open a session or read a key.
    if not calendars_enabled():
        logger.error(
            "BENZINGA_CALENDARS_ENABLED is not exactly 'true'; no network call"
        )
        return GATE_OFF

    if ns.compare_earnings:
        return await _run_compare(ns.from_date, ns.to_date, ns.symbols)

    partition_counts = {
        category: len(
            dates_in_range(
                ns.from_date - timedelta(days=endpoint_for(category).lookback_days),
                ns.to_date,
            )
        )
        for category in ns.categories
    }

    if ns.dry_run:
        summary = {
            "mode": "dry_run",
            "categories": ns.categories,
            "from_date": ns.from_date.isoformat(),
            "to_date": ns.to_date.isoformat(),
            "partitions_by_category": partition_counts,
            "symbols": ns.symbols,
            "force": ns.force,
        }
        print(json.dumps({"benzinga_calendars": summary}))
        logger.info(
            "[DRY-RUN] would ingest %s for %s..%s (partitions=%s); "
            "pass --commit to write",
            ns.categories,
            ns.from_date,
            ns.to_date,
            partition_counts,
        )
        return EXIT_OK

    try:
        api_key = read_api_key()
    except BenzingaApiKeyMissing as exc:
        logger.error("%s", exc)
        return EXIT_FAILED

    client = BenzingaCalendarClient(api_key=api_key, max_pages=ns.max_pages)

    succeeded = 0
    failed = 0
    pending = 0
    per_category: dict[str, dict[str, Any]] = {}
    async with AsyncSessionLocal() as db:
        for category in ns.categories:
            results = await ingest_benzinga_category_for_range(
                db,
                category,
                ns.from_date,
                ns.to_date,
                symbols=ns.symbols,
                client=client,
                skip_succeeded=not ns.force,
            )
            counts = {"succeeded": 0, "failed": 0, "pending": 0, "events": 0}
            for result in results:
                counts[result.status] = counts.get(result.status, 0) + 1
                counts["events"] += result.event_count
                if result.status == "succeeded":
                    succeeded += 1
                elif result.status == "pending":
                    pending += 1
                else:
                    failed += 1
                    logger.error(
                        "ingest failed for benzinga/%s/%s: %s",
                        category,
                        result.partition_date,
                        result.error,
                    )
            per_category[category] = counts

    summary = {
        "mode": "commit",
        "categories": ns.categories,
        "from_date": ns.from_date.isoformat(),
        "to_date": ns.to_date.isoformat(),
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "per_category": per_category,
    }
    print(json.dumps({"benzinga_calendars": summary}))
    logger.info("benzinga calendar ingest complete: %s", summary)
    return EXIT_OK if failed == 0 else EXIT_FAILED


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="benzinga-calendars-ingest")
    ns = parse_args(argv)
    try:
        return await run(ns)
    except Exception as exc:
        capture_exception(exc, process="ingest_benzinga_calendars")
        logger.error("ingest_benzinga_calendars crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
