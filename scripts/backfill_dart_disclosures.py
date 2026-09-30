#!/usr/bin/env python3
"""Backfill KR DART disclosures for an inclusive date range (#1085).

Read-only against DART's public filing list (``list_date_ex`` scrape via
OpenDartReader — the same fetch the scheduled daily path uses). Writes go only
through ``app.services.market_events.ingestion.ingest_kr_disclosures_for_date``,
which upserts idempotently on ``source_event_id`` (= DART ``rcept_no``) and
records one ``market_event_ingestion_partitions`` row per day.

Default mode is **dry-run**: every date in the range is fetched and reported —
date, XKRX session status, raw row count, parseable event count, current
partition state, and the predicted outcome — with no DB writes. ``--commit``
performs the actual per-day ingestion and marks partitions succeeded/failed
exactly like the scheduled CLI: a confirmed XKRX trading session that still
returns zero filings is recorded ``failed`` (never ``succeeded``), so it stays
visible and retryable.

Desk usage (after merge, on an operator host with OPENDART_API_KEY set):

    # dry-run first — per-day counts, no DB writes
    uv run python -m scripts.backfill_dart_disclosures \
        --from-date 2026-01-01 --to-date 2026-07-21

    # then write
    uv run python -m scripts.backfill_dart_disclosures \
        --from-date 2026-01-01 --to-date 2026-07-21 --commit

Exit codes: ``0`` = no day failed (commit) or is predicted to fail (dry-run);
``2`` = at least one day failed or would fail; ``1`` = the CLI itself crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cli import setup_logging_and_sentry
from app.core.db import AsyncSessionLocal
from app.models.market_events import MarketEventIngestionPartition
from app.monitoring.sentry import capture_exception
from app.services.market_events.ingestion import ingest_kr_disclosures_for_date
from app.services.market_events.normalizers import normalize_dart_disclosure_row
from app.services.market_events.session_calendar import trading_session_status
from scripts.ingest_market_events import iter_partition_dates

logger = logging.getLogger(__name__)

SOURCE = "dart"
CATEGORY = "disclosure"
MARKET = "kr"

FetchRows = Callable[[date], Awaitable[list[dict[str, Any]]]]


async def _default_fetch(target_date: date) -> list[dict[str, Any]]:
    from app.services.market_events.dart_helpers import fetch_dart_filings_for_date

    return await fetch_dart_filings_for_date(target_date)


def _parseable_count(rows: list[dict[str, Any]]) -> int:
    """Count rows the DART normalizer would accept (no DB writes)."""
    parseable = 0
    for row in rows:
        try:
            normalize_dart_disclosure_row(row)
        except ValueError:
            continue
        parseable += 1
    return parseable


def _predicted_action(session_status: str, parseable: int) -> str:
    if parseable > 0:
        return "ingest"
    if session_status == "open":
        return "fail_zero_filings_on_trading_day"
    if session_status == "closed":
        return "ok_zero_on_closed_day"
    return "fail_calendar_unknown"


async def _load_partition_states(
    db: AsyncSession,
    from_date: date,
    to_date: date,
) -> dict[date, str]:
    stmt = select(
        MarketEventIngestionPartition.partition_date,
        MarketEventIngestionPartition.status,
    ).where(
        MarketEventIngestionPartition.source == SOURCE,
        MarketEventIngestionPartition.category == CATEGORY,
        MarketEventIngestionPartition.market == MARKET,
        MarketEventIngestionPartition.partition_date >= from_date,
        MarketEventIngestionPartition.partition_date <= to_date,
    )
    rows = (await db.execute(stmt)).all()
    return dict(rows)


async def run_backfill(
    *,
    db: AsyncSession,
    from_date: date,
    to_date: date,
    commit: bool = False,
    fetch_rows: FetchRows | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Backfill DART disclosures day by day over [from_date, to_date].

    ``commit=False`` (default) is a pure dry-run: it fetches and classifies
    every day but never writes. ``commit=True`` routes each day through
    ``ingest_kr_disclosures_for_date`` and commits per day, so every partition
    row reflects a complete attempt and a crash mid-range leaves the tail
    untouched rather than half-marked.

    ``fetch_rows`` is an injection point for tests; the production default is
    ``dart_helpers.fetch_dart_filings_for_date``.
    """
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    if fetch_rows is None:
        fetch_rows = _default_fetch

    dates = list(iter_partition_dates(from_date, to_date))
    partition_states = await _load_partition_states(db, from_date, to_date)

    succeeded = 0
    failed = 0
    for d in dates:
        session_status = trading_session_status(MARKET, d)
        partition_state = partition_states.get(d, "none")

        if not commit:
            rows = await fetch_rows(d)
            parseable = _parseable_count(rows)
            action = _predicted_action(session_status, parseable)
            emit(
                f"{d.isoformat()} session={session_status} rows={len(rows)} "
                f"parseable={parseable} partition={partition_state} "
                f"action={action}"
            )
            if action.startswith("fail"):
                failed += 1
            else:
                succeeded += 1
            continue

        result = await ingest_kr_disclosures_for_date(db, d, fetch_rows=fetch_rows)
        await db.commit()
        emit(
            f"{d.isoformat()} status={result.status} events={result.event_count}"
            + (f" error={result.error}" if result.error else "")
        )
        if result.status == "succeeded":
            succeeded += 1
        else:
            failed += 1
            logger.error(
                "dart backfill failed for %s on %s: %s",
                f"{SOURCE}/{CATEGORY}/{MARKET}",
                d,
                result.error,
            )

    summary = {
        "source": SOURCE,
        "category": CATEGORY,
        "market": MARKET,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "dry_run": not commit,
        "succeeded": succeeded,
        "failed": failed,
    }
    emit(json.dumps(summary))
    logger.info("dart backfill complete: %s", summary)
    return 0 if failed == 0 else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill KR DART disclosures over an inclusive date range. "
            "Default is --dry-run (per-day counts, no DB writes); --commit "
            "writes through the standard ingestion path."
        )
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        dest="from_date",
        required=True,
        help="ISO start date (inclusive).",
    )
    parser.add_argument(
        "--to-date",
        type=date.fromisoformat,
        dest="to_date",
        required=True,
        help="ISO end date (inclusive).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_false",
        dest="commit",
        help="Fetch and report per-day counts without writing (default).",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        dest="commit",
        help="Write events and partition rows through the ingestion path.",
    )
    parser.set_defaults(commit=False)
    ns = parser.parse_args(argv)
    if ns.from_date > ns.to_date:
        parser.error("--from-date must be less than or equal to --to-date")
    return ns


async def main(argv: list[str] | None = None) -> int:
    setup_logging_and_sentry(service_name="dart-backfill")
    ns = parse_args(argv)

    try:
        async with AsyncSessionLocal() as db:
            return await run_backfill(
                db=db,
                from_date=ns.from_date,
                to_date=ns.to_date,
                commit=ns.commit,
            )
    except Exception as exc:
        capture_exception(exc, process="backfill_dart_disclosures")
        logger.error("backfill_dart_disclosures crashed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
