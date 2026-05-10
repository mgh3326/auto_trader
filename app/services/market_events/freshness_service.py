"""Read-only freshness + coverage queries against market_event_ingestion_partitions (ROB-167).

This service NEVER writes. It is the canonical source of truth for the
diagnostic surfaces in `/invest/api/calendar` (CalendarMeta) and
`GET /trading/api/market-events/coverage`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventIngestionPartition
from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarDayState,
    CalendarSourceState,
    CalendarSourceStatus,
    CoverageMatrixResponse,
    CoveragePartitionRow,
)
from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)

# Partition is considered stale if its `finished_at` is older than this window.
# Sized for the recommended Prefect rolling window (today-7 .. today+60) being
# refreshed at least every 24 h.
STALE_AFTER_HOURS = 36


def _is_stale(finished_at: datetime | None, *, now: datetime) -> bool:
    if finished_at is None:
        return False
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return finished_at < (now - timedelta(hours=STALE_AFTER_HOURS))


def _date_iter(from_date: date, to_date: date):
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


class MarketEventsFreshnessService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_partitions(
        self, from_date: date, to_date: date
    ) -> list[MarketEventIngestionPartition]:
        stmt = (
            select(MarketEventIngestionPartition)
            .where(
                MarketEventIngestionPartition.partition_date >= from_date,
                MarketEventIngestionPartition.partition_date <= to_date,
            )
            .order_by(
                MarketEventIngestionPartition.partition_date.asc(),
                MarketEventIngestionPartition.source.asc(),
            )
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_per_day_states(
        self, from_date: date, to_date: date, *, now: datetime | None = None
    ) -> dict[date, CalendarDayState]:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        now = now or datetime.now(UTC)
        rows = await self._load_partitions(from_date, to_date)

        by_date: dict[date, list[MarketEventIngestionPartition]] = defaultdict(list)
        for r in rows:
            by_date[r.partition_date].append(r)

        out: dict[date, CalendarDayState] = {}
        for d in _date_iter(from_date, to_date):
            expected = expected_sources_for_date(d)
            present = by_date.get(d, [])
            present_keys = {(p.source, p.category, p.market) for p in present}

            if not present:
                out[d] = "missing"
                continue

            missing = expected - present_keys
            failed = [p for p in present if p.status == "failed"]
            running_or_pending = [
                p for p in present if p.status in ("running", "pending")
            ]
            succeeded = [p for p in present if p.status == "succeeded"]

            if failed:
                out[d] = "error"
                continue
            if missing or running_or_pending:
                out[d] = "partial"
                continue
            # All present and all succeeded
            if all(_is_stale(p.finished_at, now=now) for p in succeeded):
                out[d] = "stale"
                continue
            if all(p.event_count == 0 for p in succeeded):
                out[d] = "empty"
                continue
            out[d] = "loaded"

        return out

    async def get_coverage_matrix(
        self, from_date: date, to_date: date, *, now: datetime | None = None
    ) -> CoverageMatrixResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        now = now or datetime.now(UTC)
        rows = await self._load_partitions(from_date, to_date)

        by_source: dict[tuple[str, str, str], dict[str, object]] = {}

        for triple in EXPECTED_SOURCES:
            by_source[triple] = {
                "succeeded": 0,
                "failed": 0,
                "event_count": 0,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error": None,
            }
        for r in rows:
            triple = (r.source, r.category, r.market)
            agg = by_source.setdefault(
                triple,
                {
                    "succeeded": 0,
                    "failed": 0,
                    "event_count": 0,
                    "last_success_at": None,
                    "last_failure_at": None,
                    "last_error": None,
                },
            )
            if r.status == "succeeded":
                agg["succeeded"] = int(agg["succeeded"]) + 1
                agg["event_count"] = int(agg["event_count"]) + (r.event_count or 0)
                cur = agg["last_success_at"]
                if r.finished_at is not None and (cur is None or r.finished_at > cur):
                    agg["last_success_at"] = r.finished_at
            elif r.status == "failed":
                agg["failed"] = int(agg["failed"]) + 1
                cur = agg["last_failure_at"]
                if r.finished_at is not None and (cur is None or r.finished_at > cur):
                    agg["last_failure_at"] = r.finished_at
                    agg["last_error"] = r.last_error

        expected_per_triple: dict[tuple[str, str, str], int] = defaultdict(int)
        for d in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(d):
                expected_per_triple[triple] += 1

        sources: list[CalendarSourceStatus] = []
        total_expected = 0
        total_succeeded = 0
        total_failed = 0
        total_missing = 0
        total_events = 0

        for triple in sorted(by_source.keys()):
            src, cat, mkt = triple
            agg = by_source[triple]
            expected_count = expected_per_triple.get(triple, 0)
            succeeded_count = int(agg["succeeded"])
            failed_count = int(agg["failed"])
            missing_count = max(0, expected_count - succeeded_count - failed_count)
            event_count = int(agg["event_count"])
            last_success_at: datetime | None = agg["last_success_at"]  # type: ignore[assignment]
            last_failure_at: datetime | None = agg["last_failure_at"]  # type: ignore[assignment]

            state: CalendarSourceState
            if failed_count > 0:
                state = "failed"
            elif succeeded_count == 0:
                state = "missing"
            elif last_success_at is not None and _is_stale(last_success_at, now=now):
                state = "stale"
            else:
                state = "fresh"

            sources.append(
                CalendarSourceStatus(
                    source=src,
                    category=cat,
                    market=mkt,
                    state=state,
                    lastSuccessAt=last_success_at,
                    lastFailureAt=last_failure_at,
                    lastError=str(agg["last_error"])
                    if agg["last_error"] is not None
                    else None,
                    succeededPartitions=succeeded_count,
                    failedPartitions=failed_count,
                    missingPartitions=missing_count,
                    eventCount=event_count,
                )
            )
            total_expected += expected_count
            total_succeeded += succeeded_count
            total_failed += failed_count
            total_missing += missing_count
            total_events += event_count

        partitions: list[CoveragePartitionRow] = []
        present_keys: set[tuple[date, str, str, str]] = set()
        for r in rows:
            present_keys.add((r.partition_date, r.source, r.category, r.market))
            partitions.append(
                CoveragePartitionRow(
                    source=r.source,
                    category=r.category,
                    market=r.market,
                    partitionDate=r.partition_date,
                    status=r.status,  # type: ignore[arg-type]
                    eventCount=r.event_count,
                    startedAt=r.started_at,
                    finishedAt=r.finished_at,
                    lastError=r.last_error,
                    retryCount=r.retry_count,
                )
            )
        for d in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(d):
                key = (d, triple[0], triple[1], triple[2])
                if key in present_keys:
                    continue
                partitions.append(
                    CoveragePartitionRow(
                        source=triple[0],
                        category=triple[1],
                        market=triple[2],
                        partitionDate=d,
                        status="expected_missing",
                    )
                )
        partitions.sort(key=lambda p: (p.partitionDate, p.source, p.category, p.market))

        actual_event_count = (
            await self.db.execute(
                select(func.count())
                .select_from(MarketEvent)
                .where(
                    MarketEvent.event_date >= from_date,
                    MarketEvent.event_date <= to_date,
                )
            )
        ).scalar_one()

        coverage = CalendarCoverage(
            fromDate=from_date,
            toDate=to_date,
            expectedPartitions=total_expected,
            succeededPartitions=total_succeeded,
            failedPartitions=total_failed,
            missingPartitions=total_missing,
            totalEvents=int(actual_event_count),
        )

        return CoverageMatrixResponse(
            fromDate=from_date,
            toDate=to_date,
            asOf=now,
            sources=sources,
            partitions=partitions,
            coverage=coverage,
        )
