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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today_kst
from app.schemas.market_events import IngestionRunResult
from app.services.market_events.dart_helpers import (
    dart_row_stock_code,
    fetch_dart_corp_registry,
    join_receipt_times,
)
from app.services.market_events.finnhub_helpers import fetch_earnings_calendar_finnhub
from app.services.market_events.forexfactory_helpers import ForexFactoryFetchError
from app.services.market_events.normalizers import (
    normalize_dart_disclosure_row,
    normalize_finnhub_earnings_row,
)
from app.services.market_events.repository import MarketEventsRepository
from app.services.market_events.session_calendar import trading_session_status

logger = logging.getLogger(__name__)


def _require_legitimate_dart_zero(
    target_date: date,
    event_count: int,
    *,
    may_be_pending: bool = False,
) -> bool:
    """Reject an empty DART result unless the day is legitimately empty or unfiled.

    Returns ``True`` when the zero result is *pending*: the caller said the day is
    not complete yet (``may_be_pending``) and XKRX confirms it is a trading
    session, so DART simply has not published the list. The caller must not record
    the partition as complete. Returns ``False`` when the result is usable (rows
    present, or XKRX confirms a non-session). Every other zero raises, including
    the unknown-calendar branch, which ``may_be_pending`` never relaxes.
    """
    if event_count > 0:
        return False

    session_status = trading_session_status("kr", target_date)
    if session_status == "closed":
        logger.info(
            "DART returned zero filings on confirmed XKRX non-session %s",
            target_date,
        )
        return False
    if session_status == "open":
        if may_be_pending:
            logger.info(
                "DART returned zero filings for %s, which is not complete yet in "
                "KST; leaving the partition pending for a later run",
                target_date,
            )
            return True
        raise RuntimeError(
            f"DART returned zero filings on confirmed XKRX trading session "
            f"{target_date}"
        )
    raise RuntimeError(
        f"DART returned zero filings for {target_date}, but XKRX calendar "
        "classification is unknown; cannot distinguish a legitimate empty day "
        "from a broken DART scraper"
    )


async def _mark_failed_after_exception(
    db: AsyncSession,
    *,
    source: str,
    category: str,
    market: str,
    partition_date: date,
    error: Exception,
    failure_code: str | None = None,
) -> IngestionRunResult:
    """Record a failed partition after any fetch/normalization/upsert exception.

    Database exceptions can leave the current transaction unusable, so rollback first
    and then write the failed partition state in a clean transaction.

    ``failure_code`` (a closed v2 code) is recorded instead of the message when a
    collector supplies one.
    """
    await db.rollback()
    repo = MarketEventsRepository(db)
    partition = await repo.get_or_create_partition(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
    )
    if failure_code is not None:
        await repo.mark_partition_failed(partition, failure_code=failure_code)
        message = failure_code
    else:
        message = str(error)
        await repo.mark_partition_failed(partition, error=message)
    return IngestionRunResult(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
        status="failed",
        event_count=0,
        error=message,
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

    fetch_from = min(dates_to_process)
    fetch_to = max(dates_to_process)
    response = await fetch_earnings_calendar_finnhub(
        None, fetch_from.isoformat(), fetch_to.isoformat()
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
    *,
    today: date | None = None,
    enrich_capital: bool | None = None,
    load_corp_registry: Callable[[], Awaitable[dict[str, list[dict[str, str]]] | None]]
    | None = None,
    load_receipt_times: Callable[..., Awaitable[dict[str, str] | None]] | None = None,
) -> IngestionRunResult:
    """Ingest KR DART disclosures for one day.

    `fetch_rows` is an optional injection point: an async callable taking a date and
    returning a list of dart-row dicts. Default uses
    `app.services.market_events.dart_helpers.fetch_dart_filings_for_date` (the
    official list.json, hk 6408).

    `load_receipt_times` enriches those official rows with the same day's scraped
    receipt times, joined by exact receipt number (hk 6968). It defaults to the
    production scrape only when `fetch_rows` is also defaulted, so an injected
    fetcher never reaches the network. A failing enrichment is fail-open: the
    official rows store date-only.

    With a custom fetcher, typed detail enrichment is opt-in via `enrich_capital`.

    `today` (default: today in Asia/Seoul) injects the clock. A date on or after
    `today` is not complete yet: DART publishes the day's list during the day, so
    a zero-row fetch on an XKRX trading session returns status ``pending`` and
    leaves the partition ``pending`` (never ``succeeded``) so a later run fetches
    it again. Dates before `today` keep the fail-closed zero-filings guard.
    """
    from app.services.market_events.dart_capital import (
        DartCredentialError,
        DartDetailError,
        capital_endpoint,
        enrich_capital_event,
    )

    if enrich_capital is None:
        enrich_capital = fetch_rows is None
    production_fetch = fetch_rows is None
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
        if load_receipt_times is None and production_fetch:
            from app.services.market_events.dart_helpers import (
                fetch_dart_receipt_times_for_date,
            )

            load_receipt_times = fetch_dart_receipt_times_for_date
        enrichment_failed = False
        if load_receipt_times is not None:
            try:
                expected = frozenset(
                    str(row.get("rcept_no") or "").strip() for row in rows
                ) - {""}
                times = await load_receipt_times(
                    target_date, expected_receipts=expected
                )
            except Exception as exc:
                logger.warning(
                    "DART receipt-time enrichment failed (%s); rows stay date-only",
                    type(exc).__name__,
                )
                times = None
            # A failed lookup is not "no timed rows": keep the day retryable below.
            enrichment_failed = times is None
            rows, applied = join_receipt_times(rows, times or {})
            logger.info(
                "DART receipt times applied to %d of %d official rows for %s",
                applied,
                len(rows),
                target_date,
            )
        # Our first-receipt clock, stamped after the provider answered. The
        # repository writes it on insert only, so a re-ingest never moves it.
        received_at = datetime.now(UTC)
        corp_registry = None
        endpoints = [capital_endpoint(row.get("report_nm") or "") for row in rows]
        needs_registry = any(
            row.get("corp_name")
            and (
                not dart_row_stock_code(row)
                or (enrich_capital and endpoint and not row.get("corp_code"))
            )
            for row, endpoint in zip(rows, endpoints, strict=True)
        )
        if needs_registry:
            if load_corp_registry is None:
                load_corp_registry = fetch_dart_corp_registry
            try:
                corp_registry = await load_corp_registry()
            except Exception:
                # Registry lookup is enrichment only; ingestion must continue.
                logger.warning("DART corp registry unavailable")
        upserted = 0
        fiscal_months: dict[str, str | None] = {}
        detail_error: DartDetailError | None = None
        credential_refused = False
        for row, endpoint in zip(rows, endpoints, strict=True):
            try:
                event_dict, value_dicts = normalize_dart_disclosure_row(
                    row, corp_registry=corp_registry
                )
            except ValueError as exc:
                logger.warning("skipping unparseable dart row: %s (%s)", row, exc)
                continue
            event_dict["first_received_at"] = received_at
            if enrich_capital and endpoint and not credential_refused:
                try:
                    value_dicts = await enrich_capital_event(
                        endpoint, row, event_dict, corp_registry, fiscal_months
                    )
                except DartDetailError as exc:
                    credential_refused = isinstance(exc, DartCredentialError)
                    if detail_error is None or credential_refused:
                        detail_error = exc
                    logger.warning(
                        "DART detail lookup failed for receipt %s: %s",
                        event_dict["source_event_id"],
                        exc,
                    )
            if endpoint and not value_dicts:
                await repo.upsert_event_with_values(
                    event_dict, value_dicts, preserve_dart_detail=True
                )
            else:
                await repo.upsert_event_with_values(event_dict, value_dicts)
            upserted += 1

        if detail_error is not None:
            await repo.mark_partition_failed(
                partition, failure_code=detail_error.failure_code
            )
            return IngestionRunResult(
                source=source,
                category=partition_category,
                market=market,
                partition_date=target_date,
                status="failed",
                event_count=upserted,
                error=detail_error.failure_code,
            )

        day_incomplete = not rows and target_date >= (today or today_kst())
        if _require_legitimate_dart_zero(
            target_date, upserted, may_be_pending=day_incomplete
        ):
            await repo.mark_partition_pending(partition)
            return IngestionRunResult(
                source=source,
                category=partition_category,
                market=market,
                partition_date=target_date,
                status="pending",
                event_count=0,
            )
        if enrichment_failed and upserted:
            # The rows are stored date-only, but the receipt times were never
            # looked up. Leave the day pending so a later run re-collects it, and
            # the minute trigger does not silently skip its disclosures (hk 6408).
            await repo.mark_partition_pending(partition)
            return IngestionRunResult(
                source=source,
                category=partition_category,
                market=market,
                partition_date=target_date,
                status="pending",
                event_count=upserted,
                error="receipt_time_unavailable",
            )
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
        # A collection failure records its closed code; other failures keep the
        # message this module wrote (zero-day guard, identity conflict).
        return await _mark_failed_after_exception(
            db,
            source=source,
            category=partition_category,
            market=market,
            partition_date=target_date,
            error=exc,
            failure_code=(
                exc.failure_code if isinstance(exc, DartDetailError) else None
            ),
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
