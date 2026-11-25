"""Per-category Benzinga calendar ingestion (hk 6513).

One API fetch per category per run, then one partition row per event date —
the same shape ``ingest_us_earnings_for_range`` uses. Rows land in
``market_events`` through :class:`MarketEventsRepository` only; this module
never writes SQL.

Idempotency and revisions:

* A record's canonical payload hash is compared with the newest row in
  ``market_event_revisions``. Equal → nothing is written (event values,
  ``first_received_at`` and ``updated_at`` stay untouched).
* Different → one revision row is appended (the previous version remains) and
  the current ``market_events`` row is updated. Nothing is overwritten
  silently.

The fetch, the repository and the session are injectable so the idempotency
rules can be exercised without a database (``db=None`` skips commit/rollback).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.market_events import IngestionRunResult
from app.services.benzinga_calendars.client import BenzingaCalendarClient
from app.services.benzinga_calendars.normalizers import (
    MARKET,
    SOURCE,
    NormalizedRow,
    content_hash,
    normalize_row,
)
from app.services.benzinga_calendars.spec import endpoint_for
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)


def dates_in_range(from_date: date, to_date: date) -> list[date]:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    out: list[date] = []
    cursor = from_date
    while cursor <= to_date:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _group_rows(
    category: str,
    rows: list[dict],
    *,
    dates_to_process: list[date],
    observed_at: datetime,
) -> tuple[dict[date, list[tuple[dict, NormalizedRow]]], int]:
    """Normalize and bucket rows by event date (pure apart from logging)."""
    grouped: dict[date, list[tuple[dict, NormalizedRow]]] = {
        d: [] for d in dates_to_process
    }
    out_of_window = 0
    for raw_row in rows:
        try:
            normalized = normalize_row(
                category,
                raw_row,
                first_received_at=observed_at,
                fetched_at=observed_at,
            )
        except ValueError as exc:
            logger.warning("skipping unparseable benzinga %s row: %s", category, exc)
            continue
        if normalized is None:
            continue
        bucket = grouped.get(normalized.event["event_date"])
        if bucket is None:
            out_of_window += 1
            continue
        bucket.append((raw_row, normalized))
    return grouped, out_of_window


async def ingest_benzinga_category_for_range(
    db: AsyncSession | None,
    category: str,
    from_date: date,
    to_date: date,
    *,
    symbols: str | None = None,
    client: BenzingaCalendarClient | None = None,
    repo: MarketEventsRepository | None = None,
    fetched_at: datetime | None = None,
    skip_succeeded: bool = True,
) -> list[IngestionRunResult]:
    """Fetch one category for [from_date, to_date] and write one partition/day.

    Raises ``ValueError`` for an unknown category, a reversed range or a
    missing client, and any client error (gate, key, HTTP, contract, page cap)
    unchanged — every affected partition is first recorded as failed, so a
    truncated or failed read never looks like an empty day.
    """
    endpoint_for(category)  # unknown category fails before any DB work
    all_dates = dates_in_range(from_date, to_date)
    repository = repo
    if repository is None:
        if db is None:
            raise ValueError("a repository or an async session is required")
        repository = MarketEventsRepository(db)

    succeeded: set[date] = set()
    if skip_succeeded:
        succeeded = await repository.list_succeeded_partitions_in_range(
            source=SOURCE,
            category=category,
            market=MARKET,
            from_date=from_date,
            to_date=to_date,
        )
    dates_to_process = [d for d in all_dates if d not in succeeded]
    if not dates_to_process:
        logger.info(
            "all %d benzinga %s partitions already succeeded for %s..%s; "
            "skipping fetch",
            len(all_dates),
            category,
            from_date,
            to_date,
        )
        return []

    if client is None:
        raise ValueError("a BenzingaCalendarClient is required")

    now = fetched_at or datetime.now(UTC)
    fetch_from = min(dates_to_process)
    fetch_to = max(dates_to_process)

    try:
        rows = await client.fetch(
            category,
            from_date=fetch_from,
            to_date=fetch_to,
            symbols=symbols,
        )
    except Exception as exc:
        for failed_date in dates_to_process:
            partition = await repository.get_or_create_partition(
                source=SOURCE,
                category=category,
                market=MARKET,
                partition_date=failed_date,
            )
            await repository.mark_partition_failed(partition, error=str(exc))
        if db is not None:
            await db.commit()
        raise

    grouped, out_of_window = _group_rows(
        category, rows, dates_to_process=dates_to_process, observed_at=now
    )
    if out_of_window:
        logger.info(
            "%d benzinga %s rows fell outside the processed partitions",
            out_of_window,
            category,
        )

    results: list[IngestionRunResult] = []
    for target_date in dates_to_process:
        partition = await repository.get_or_create_partition(
            source=SOURCE,
            category=category,
            market=MARKET,
            partition_date=target_date,
        )
        await repository.mark_partition_running(partition)
        day_rows = grouped[target_date]
        try:
            written = await _write_day(repository, day_rows, observed_at=now)
            await repository.mark_partition_succeeded(
                partition, event_count=len(day_rows)
            )
            if db is not None:
                await db.commit()
            logger.info(
                "benzinga %s %s: %d rows (%d written this run)",
                category,
                target_date,
                len(day_rows),
                written,
            )
            results.append(
                IngestionRunResult(
                    source=SOURCE,
                    category=category,
                    market=MARKET,
                    partition_date=target_date,
                    status="succeeded",
                    event_count=len(day_rows),
                )
            )
        except Exception as exc:
            logger.exception(
                "benzinga %s ingestion failed for %s", category, target_date
            )
            if db is not None:
                await db.rollback()
            failed_partition = await repository.get_or_create_partition(
                source=SOURCE,
                category=category,
                market=MARKET,
                partition_date=target_date,
            )
            await repository.mark_partition_failed(failed_partition, error=str(exc))
            if db is not None:
                await db.commit()
            results.append(
                IngestionRunResult(
                    source=SOURCE,
                    category=category,
                    market=MARKET,
                    partition_date=target_date,
                    status="failed",
                    event_count=0,
                    error=str(exc),
                )
            )
    return results


async def _write_day(
    repository: MarketEventsRepository,
    day_rows: list[tuple[dict, NormalizedRow]],
    *,
    observed_at: datetime,
) -> int:
    """Write one day's rows; return how many were actually persisted."""
    written = 0
    for raw_row, normalized in day_rows:
        event = normalized.event
        source_event_id = event.get("source_event_id")
        if not source_event_id:
            raise ValueError("benzinga row has no source_event_id")
        digest = content_hash(raw_row)
        latest = await repository.latest_revision(
            source=SOURCE,
            category=event["category"],
            market=MARKET,
            source_event_id=source_event_id,
        )
        if latest is not None and latest.content_hash == digest:
            continue
        appended = await repository.append_revision(
            source=SOURCE,
            category=event["category"],
            market=MARKET,
            source_event_id=source_event_id,
            content_hash=digest,
            observed_at=observed_at,
            provider_created_at=event.get("provider_created_at"),
            provider_updated_at=event.get("provider_updated_at"),
            raw_payload_json=event.get("raw_payload_json"),
        )
        if not appended:
            # The observation was not recorded (a concurrent writer took this
            # sequence number). Updating the current row anyway would leave the
            # revision log disagreeing with the state it explains.
            raise RuntimeError(
                f"benzinga revision for {source_event_id} was not recorded"
            )
        await repository.upsert_event_with_values(event, normalized.values)
        written += 1
    return written


__all__ = [
    "dates_in_range",
    "ingest_benzinga_category_for_range",
]
