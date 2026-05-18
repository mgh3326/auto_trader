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
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.market_events import IngestionRunResult
from app.services.market_events.finnhub_helpers import fetch_earnings_calendar_finnhub
from app.services.market_events.forexfactory_helpers import ForexFactoryFetchError
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


async def ingest_us_earnings_for_range(
    db: AsyncSession,
    from_date: date,
    to_date: date,
    *,
    skip_succeeded: bool = True,
) -> list[IngestionRunResult]:
    """Range-aware US earnings ingestion (ROB-264).

    Calls Finnhub once for the entire [from_date, to_date] window, groups rows
    by `event_date`, and writes one `market_event_ingestion_partitions` row per
    day in the window — including days with zero events.

    When `skip_succeeded=True` (default), days already marked
    `status='succeeded'` in the partition table are left untouched. Use
    `skip_succeeded=False` for explicit replay.

    Raises:
        FinnhubQuotaExceededError: 429 from Finnhub. No partitions are mutated;
            callers should fail closed and retry after quota resets.
    """
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    source = "finnhub"
    category = "earnings"
    market = "us"
    repo = MarketEventsRepository(db)

    all_dates: list[date] = []
    cur = from_date
    while cur <= to_date:
        all_dates.append(cur)
        cur += timedelta(days=1)

    succeeded_set: set[date] = set()
    if skip_succeeded:
        succeeded_set = await repo.list_succeeded_partitions_in_range(
            source=source,
            category=category,
            market=market,
            from_date=from_date,
            to_date=to_date,
        )

    dates_to_process = [d for d in all_dates if d not in succeeded_set]
    if not dates_to_process:
        logger.info(
            "all %d partitions already succeeded for %s..%s; skipping fetch",
            len(all_dates),
            from_date,
            to_date,
        )
        return []

    response = await fetch_earnings_calendar_finnhub(
        None, from_date.isoformat(), to_date.isoformat()
    )
    rows = response.get("earnings", []) if isinstance(response, dict) else []

    rows_by_date: dict[date, list[dict[str, Any]]] = {d: [] for d in dates_to_process}
    for row in rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            ev_date = date.fromisoformat(raw_date)
        except ValueError:
            logger.warning("skipping finnhub row with bad date: %s", row)
            continue
        if ev_date in rows_by_date:
            rows_by_date[ev_date].append(row)

    results: list[IngestionRunResult] = []
    for d in dates_to_process:
        partition = await repo.get_or_create_partition(
            source=source,
            category=category,
            market=market,
            partition_date=d,
        )
        await repo.mark_partition_running(partition)
        try:
            upserted = 0
            for row in rows_by_date[d]:
                try:
                    event_dict, value_dicts = normalize_finnhub_earnings_row(row)
                except ValueError as exc:
                    logger.warning(
                        "skipping unparseable finnhub row: %s (%s)", row, exc
                    )
                    continue
                await repo.upsert_event_with_values(event_dict, value_dicts)
                upserted += 1
            await repo.mark_partition_succeeded(partition, event_count=upserted)
            await db.commit()
            results.append(
                IngestionRunResult(
                    source=source,
                    category=category,
                    market=market,
                    partition_date=d,
                    status="succeeded",
                    event_count=upserted,
                )
            )
        except Exception as exc:
            logger.exception("finnhub earnings ingestion failed for %s", d)
            failed = await _mark_failed_after_exception(
                db,
                source=source,
                category=category,
                market=market,
                partition_date=d,
                error=exc,
            )
            await db.commit()
            results.append(failed)

    return results


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


async def ingest_kr_earnings_wisefn_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest WiseFn KR earnings rows for one day (ROB-171).

    `fetch_rows` is an optional injection point. Default uses
    `app.services.market_events.wisefn_helpers.fetch_wisefn_earnings_for_date`,
    which currently raises NotImplementedError until the upstream contract is
    confirmed. Tests inject a fixture-returning AsyncMock.
    """
    if fetch_rows is None:
        from app.services.market_events.wisefn_helpers import (
            fetch_wisefn_earnings_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "wisefn"
    category = "earnings"
    market = "kr"
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
            normalize_wisefn_earnings_row,
        )

        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_wisefn_earnings_row(row)
            except ValueError as exc:
                logger.warning("skipping unparseable wisefn row: %s (%s)", row, exc)
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
        logger.exception("wisefn earnings ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
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

        if rows is None:
            await db.rollback()
            repo2 = MarketEventsRepository(db)
            partition2 = await repo2.get_or_create_partition(
                source=source,
                category=category,
                market=market,
                partition_date=target_date,
            )
            await repo2.mark_partition_failed(
                partition2, error="forexfactory_out_of_rolling_window"
            )
            return IngestionRunResult(
                source=source,
                category=category,
                market=market,
                partition_date=target_date,
                status="failed",
                event_count=0,
                error="forexfactory_out_of_rolling_window",
            )

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
    except ForexFactoryFetchError as exc:
        logger.warning("forexfactory fetch failed for %s: %s", target_date, exc.reason)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=Exception(f"forexfactory_{exc.reason}"),
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


async def ingest_tradingview_economic_events_for_date(
    db: AsyncSession,
    target_date: date,
    fetch_rows: Callable[[date], Awaitable[list[dict[str, Any]]]] | None = None,
) -> IngestionRunResult:
    """Ingest TradingView economic-calendar events for one day.

    `fetch_rows` is an optional injection point. Default uses
    `app.services.market_events.tradingview_helpers.fetch_tradingview_events_for_date`.
    """
    if fetch_rows is None:
        from app.services.market_events.tradingview_helpers import (
            fetch_tradingview_events_for_date as _default_fetch,
        )

        fetch_rows = _default_fetch

    source = "tradingview"
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
            normalize_tradingview_event_row,
        )

        rows = await fetch_rows(target_date)
        upserted = 0
        for row in rows:
            try:
                event_dict, value_dicts = normalize_tradingview_event_row(row)
            except ValueError as exc:
                logger.warning(
                    "skipping unparseable tradingview row: %s (%s)", row, exc
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
        logger.exception("tradingview ingestion failed for %s", target_date)
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=category,
            market=market,
            partition_date=target_date,
            error=exc,
        )
