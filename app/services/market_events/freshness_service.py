"""Read-only freshness + coverage queries against market_event_ingestion_partitions.

This service NEVER writes. It backs the ROB-167 coverage surfaces and the
ROB-208 rolling scheduler/freshness diagnostics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
from app.schemas.market_events_freshness import (
    MarketEventsFreshnessResponse,
    MarketEventsFreshnessRow,
)
from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)
from app.services.market_events.session_calendar import Market, trading_session_status

# Partition is considered stale for the ROB-167 per-day/coverage surfaces if its
# `finished_at` is older than this window. Sized for the recommended rolling
# window (today-7 .. today+60) being refreshed at least every 24 h.
STALE_AFTER_HOURS = 36

# Default threshold for the ROB-208 source/category/market freshness endpoint.
DEFAULT_STALE_THRESHOLD_HOURS = 30.0


@dataclass(frozen=True)
class _Key:
    source: str
    category: str
    market: str


_MARKET_ZONES: dict[str, tuple[Market, str]] = {
    "kr": ("kr", "Asia/Seoul"),
    "us": ("us", "America/New_York"),
}


def _is_stale(
    finished_at: datetime | None, *, now: datetime, market: str | None = None
) -> bool:
    if finished_at is None:
        return False
    finished_at = _ensure_aware(finished_at)
    extra_hours = 24 * _non_session_days(market, finished_at, now) if market else 0
    return finished_at < (now - timedelta(hours=STALE_AFTER_HOURS + extra_hours))


def _non_session_days(market: str | None, finished_at: datetime, now: datetime) -> int:
    """Count closed exchange days after the last success and before today (local).

    Today is never counted: counting it would add 24 hours right after local
    midnight and hide a missed run on the trading day that just ended.
    An `unknown` calendar answer counts as open, so a calendar failure keeps
    the plain hour rule instead of extending the threshold.
    """
    mapping = _MARKET_ZONES.get(market or "")
    if mapping is None:
        return 0
    calendar_market, zone_name = mapping
    zone = ZoneInfo(zone_name)
    day = _ensure_aware(finished_at).astimezone(zone).date() + timedelta(days=1)
    last = _ensure_aware(now).astimezone(zone).date()
    count = 0
    while day < last:
        if trading_session_status(calendar_market, day) == "closed":
            count += 1
        day += timedelta(days=1)
    return count


def _date_iter(from_date: date, to_date: date) -> Iterator[date]:
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


class MarketEventsFreshnessService:
    """Compute read-only market-events ingestion freshness diagnostics."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def compute(
        self,
        *,
        window_from: date,
        window_to: date,
        stale_threshold_hours: float = DEFAULT_STALE_THRESHOLD_HOURS,
        expected_next_refresh_by_key: dict[tuple[str, str, str], datetime]
        | None = None,
        now: datetime | None = None,
    ) -> MarketEventsFreshnessResponse:
        """Return source/category/market freshness rows for a date window."""
        if window_from > window_to:
            raise ValueError("window_from must be <= window_to")
        clock = _ensure_aware(now or datetime.now(UTC))

        partitions = await self._load_partitions(window_from, window_to)
        event_counts = await self._fetch_event_counts(window_from, window_to)

        partitions_by_key: dict[_Key, list[MarketEventIngestionPartition]] = (
            defaultdict(list)
        )
        for partition in partitions:
            partitions_by_key[
                _Key(partition.source, partition.category, partition.market)
            ].append(partition)

        keys = set(partitions_by_key) | {
            _Key(source, category, market)
            for source, category, market in event_counts.keys()
        }
        expected_refresh = expected_next_refresh_by_key or {}
        rows = [
            self._row_for_key(
                key=key,
                partitions=partitions_by_key.get(key, []),
                event_count=event_counts.get((key.source, key.category, key.market), 0),
                window_from=window_from,
                window_to=window_to,
                stale_threshold_hours=stale_threshold_hours,
                expected_next_refresh=expected_refresh.get(
                    (key.source, key.category, key.market)
                ),
                now=clock,
            )
            for key in sorted(
                keys, key=lambda item: (item.source, item.category, item.market)
            )
        ]

        return MarketEventsFreshnessResponse(
            generated_at=clock,
            window_from=window_from,
            window_to=window_to,
            stale_threshold_hours=stale_threshold_hours,
            rows=rows,
            warnings=_build_warnings(rows, stale_threshold_hours),
        )

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

    async def _fetch_event_counts(
        self, window_from: date, window_to: date
    ) -> dict[tuple[str, str, str], int]:
        stmt = (
            select(
                MarketEvent.source,
                MarketEvent.category,
                MarketEvent.market,
                func.count(MarketEvent.id),
            )
            .where(
                MarketEvent.event_date >= window_from,
                MarketEvent.event_date <= window_to,
            )
            .group_by(MarketEvent.source, MarketEvent.category, MarketEvent.market)
        )
        return {
            (source, category, market): int(count)
            for source, category, market, count in (await self.db.execute(stmt)).all()
        }

    def _row_for_key(
        self,
        *,
        key: _Key,
        partitions: Iterable[MarketEventIngestionPartition],
        event_count: int,
        window_from: date,
        window_to: date,
        stale_threshold_hours: float,
        expected_next_refresh: datetime | None,
        now: datetime,
    ) -> MarketEventsFreshnessRow:
        plist = list(partitions)
        counts_by_status: dict[str, int] = defaultdict(int)
        for partition in plist:
            counts_by_status[partition.status] += 1

        days_in_window = (window_to - window_from).days + 1
        missing = max(0, days_in_window - len({p.partition_date for p in plist}))
        latest_succeeded = _latest_succeeded_partition(plist)
        latest_failed = max(
            (partition for partition in plist if partition.status == "failed"),
            key=lambda partition: partition.partition_date,
            default=None,
        )
        hours_since = _hours_since_finished_at(latest_succeeded, now)
        allowance_hours = 0.0
        if latest_succeeded is not None and latest_succeeded.finished_at is not None:
            allowance_hours = 24.0 * _non_session_days(
                key.market, latest_succeeded.finished_at, now
            )

        return MarketEventsFreshnessRow(
            source=key.source,
            category=key.category,
            market=key.market,
            window_from=window_from,
            window_to=window_to,
            partition_count_total=len(plist),
            partition_count_succeeded=counts_by_status.get("succeeded", 0),
            partition_count_failed=counts_by_status.get("failed", 0),
            partition_count_running=counts_by_status.get("running", 0),
            partition_count_pending=counts_by_status.get("pending", 0),
            partition_count_missing=missing,
            event_count_in_window=event_count,
            latest_succeeded_partition_date=(
                latest_succeeded.partition_date if latest_succeeded else None
            ),
            latest_succeeded_finished_at=(
                latest_succeeded.finished_at if latest_succeeded else None
            ),
            hours_since_latest_succeeded=hours_since,
            latest_failed_partition_date=(
                latest_failed.partition_date if latest_failed else None
            ),
            latest_failed_error=_truncate_error(latest_failed.last_error)
            if latest_failed
            else None,
            expected_next_refresh_at=expected_next_refresh,
            stale=(
                hours_since is None
                or hours_since > stale_threshold_hours + allowance_hours
            ),
        )

    async def get_per_day_states(
        self, from_date: date, to_date: date, *, now: datetime | None = None
    ) -> dict[date, CalendarDayState]:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        now = now or datetime.now(UTC)
        rows = await self._load_partitions(from_date, to_date)

        by_date: dict[date, list[MarketEventIngestionPartition]] = defaultdict(list)
        for row in rows:
            by_date[row.partition_date].append(row)

        out: dict[date, CalendarDayState] = {}
        for current_date in _date_iter(from_date, to_date):
            expected = expected_sources_for_date(current_date)
            present = by_date.get(current_date, [])
            present_keys = {(p.source, p.category, p.market) for p in present}

            if not present:
                out[current_date] = "missing"
                continue

            missing = expected - present_keys
            failed = [p for p in present if p.status == "failed"]
            running_or_pending = [
                p for p in present if p.status in ("running", "pending")
            ]
            succeeded = [p for p in present if p.status == "succeeded"]

            if failed:
                out[current_date] = "error"
                continue
            if missing or running_or_pending:
                out[current_date] = "partial"
                continue
            # All present and all succeeded.
            if all(
                _is_stale(p.finished_at, now=now, market=p.market) for p in succeeded
            ):
                out[current_date] = "stale"
                continue
            if all(p.event_count == 0 for p in succeeded):
                out[current_date] = "empty"
                continue
            out[current_date] = "loaded"

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
        for row in rows:
            triple = (row.source, row.category, row.market)
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
            if row.status == "succeeded":
                agg["succeeded"] = int(agg["succeeded"]) + 1
                agg["event_count"] = int(agg["event_count"]) + (row.event_count or 0)
                cur = agg["last_success_at"]
                if row.finished_at is not None and (
                    cur is None or row.finished_at > cur
                ):
                    agg["last_success_at"] = row.finished_at
            elif row.status == "failed":
                agg["failed"] = int(agg["failed"]) + 1
                cur = agg["last_failure_at"]
                if row.finished_at is not None and (
                    cur is None or row.finished_at > cur
                ):
                    agg["last_failure_at"] = row.finished_at
                    agg["last_error"] = row.last_error

        expected_per_triple: dict[tuple[str, str, str], int] = defaultdict(int)
        for current_date in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(current_date):
                expected_per_triple[triple] += 1

        sources: list[CalendarSourceStatus] = []
        total_expected = 0
        total_succeeded = 0
        total_failed = 0
        total_missing = 0

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
            elif last_success_at is not None and _is_stale(
                last_success_at, now=now, market=mkt
            ):
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

        partitions: list[CoveragePartitionRow] = []
        present_keys: set[tuple[date, str, str, str]] = set()
        for row in rows:
            present_keys.add((row.partition_date, row.source, row.category, row.market))
            partitions.append(
                CoveragePartitionRow(
                    source=row.source,
                    category=row.category,
                    market=row.market,
                    partitionDate=row.partition_date,
                    status=row.status,  # type: ignore[arg-type]
                    eventCount=row.event_count,
                    startedAt=row.started_at,
                    finishedAt=row.finished_at,
                    lastError=row.last_error,
                    retryCount=row.retry_count,
                )
            )
        for current_date in _date_iter(from_date, to_date):
            for triple in expected_sources_for_date(current_date):
                key = (current_date, triple[0], triple[1], triple[2])
                if key in present_keys:
                    continue
                partitions.append(
                    CoveragePartitionRow(
                        source=triple[0],
                        category=triple[1],
                        market=triple[2],
                        partitionDate=current_date,
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


def _latest_succeeded_partition(
    partitions: list[MarketEventIngestionPartition],
) -> MarketEventIngestionPartition | None:
    return max(
        (partition for partition in partitions if partition.status == "succeeded"),
        key=lambda partition: _ensure_aware(partition.finished_at or datetime.min),
        default=None,
    )


def _hours_since_finished_at(
    partition: MarketEventIngestionPartition | None, now: datetime
) -> float | None:
    if partition is None or partition.finished_at is None:
        return None
    finished_at = _ensure_aware(partition.finished_at)
    return round((now - finished_at).total_seconds() / 3600.0, 4)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate_error(error: str | None) -> str | None:
    if not error:
        return None
    return error[:500]


def _build_warnings(
    rows: list[MarketEventsFreshnessRow], stale_threshold_hours: float
) -> list[str]:
    warnings: list[str] = []
    for row in rows:
        key = f"{row.source}/{row.category}/{row.market}"
        if row.partition_count_failed > 0:
            warnings.append(
                f"{key}: {row.partition_count_failed} failed partition(s) in window"
            )
        if row.stale:
            warnings.append(
                f"{key}: stale (>{stale_threshold_hours:.1f}h since last success)"
            )
    return warnings
