"""Per-day ingestion orchestrators (ROB-128).

Each `ingest_*_for_date` function:
  1. claims a row in market_event_ingestion_partitions (running),
  2. fetches one day of source data,
  3. normalizes + upserts into market_events / market_event_values,
  4. marks the partition succeeded (with event_count) or failed (with last_error).

These functions are pure ingestion: no broker / order / watch / scheduling side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.market_events import IngestionRunResult
from app.services.market_events.finnhub_helpers import fetch_earnings_calendar_finnhub
from app.services.market_events.normalizers import (
    normalize_dart_disclosure_row,
    normalize_finnhub_earnings_row,
)
from app.services.market_events.repository import MarketEventsRepository

logger = logging.getLogger(__name__)


async def _mark_failed_after_exception(
    db: AsyncSession,
    *,
    source: str,
    category: str,
    market: str,
    partition_date: date,
    error: Exception,
) -> IngestionRunResult:
    """Record a failed partition after any fetch/normalization/upsert exception.

    Database exceptions can leave the current transaction unusable, so rollback first
    and then write the failed partition state in a clean transaction.
    """
    await db.rollback()
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
    )
    await repo.mark_partition_failed(partition, error=str(error))
    return IngestionRunResult(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
        status="failed",
        event_count=0,
        error=str(error),
    )


async def ingest_us_earnings_for_date(
    db: AsyncSession,
    target_date: date,
) -> IngestionRunResult:
    source = "finnhub"
    category = "earnings"
    market = "us"
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    iso = target_date.isoformat()
    try:
        response = await fetch_earnings_calendar_finnhub(None, iso, iso)
        rows = response.get("earnings", []) if isinstance(response, dict) else []
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_finnhub_earnings_row(row)
            except ValueError as exc:
                logger.warning("skipping unparseable finnhub row: %s (%s)", row, exc)
                continue
            await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        await repo.mark_partition_succeeded(partition, event_count=upserted)
        return IngestionRunResult(
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            status="succeeded",
            event_count=upserted,
        )
    except Exception as exc:
        logger.exception("finnhub earnings ingestion failed for %s", iso)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=exc,
        )


async def ingest_kr_disclosures_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest KR DART disclosures for one day.

    `fetch_rows` is an optional injection point: an async callable taking a date and
    returning a list of dart-row dicts. Default uses
    `app.services.market_events.dart_helpers.fetch_dart_filings_for_date`.
    """
    if fetch_rows is None:
        from app.services.market_events.dart_helpers import (
            fetch_dart_filings_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "dart"
    partition_category = "disclosure"
    market = "kr"
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=partition_category,
        market=market,
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    try:
        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_dart_disclosure_row(row)
            except ValueError as exc:
                logger.warning("skipping unparseable dart row: %s (%s)", row, exc)
                continue
            await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        await repo.mark_partition_succeeded(partition, event_count=upserted)
        return IngestionRunResult(
            source=source,
            category=partition_category,
            market=market,
            partition_date=target_date,
            status="succeeded",
            event_count=upserted,
        )
    except Exception as exc:
        logger.exception("dart ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=partition_category,
            market=market,
            partition_date=target_date,
            error=exc,
        )


async def ingest_economic_events_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest ForexFactory economic-calendar events for one day.

    `fetch_rows` is an optional injection point. Default uses
    `app.services.market_events.forexfactory_helpers.fetch_forexfactory_events_for_date`.
    """
    if fetch_rows is None:
        from app.services.market_events.forexfactory_helpers import (
            fetch_forexfactory_events_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "forexfactory"
    category = "economic"
    market = "global"
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=target_date,
    )
    await repo.mark_partition_running(partition)

    try:
        from app.services.market_events.normalizers import (
            normalize_forexfactory_event_row,
        )

        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_forexfactory_event_row(row)
            except ValueError as exc:
                logger.warning(
                    "skipping unparseable forexfactory row: %s (%s)", row, exc
                )
                continue
            await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        await repo.mark_partition_succeeded(partition, event_count=upserted)
        return IngestionRunResult(
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            status="succeeded",
            event_count=upserted,
        )
    except Exception as exc:
        logger.exception("forexfactory ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=exc,
        )
