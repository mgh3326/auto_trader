"""Tests for read-only market-events freshness diagnostics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.freshness_service import (
    DEFAULT_STALE_THRESHOLD_HOURS,
    STALE_AFTER_HOURS,
    MarketEventsFreshnessService,
)
from tests.market_events_test_helpers import market_events_test_lock


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock(request):
    if request.node.get_closest_marker("integration") is None:
        yield
        return
    async with market_events_test_lock():
        yield


def _add_partition(
    db: AsyncSession,
    *,
    source: str,
    category: str,
    market: str,
    partition_date: date,
    status: str,
    event_count: int = 0,
    finished_at: datetime | None = None,
    last_error: str | None = None,
) -> MarketEventIngestionPartition:
    row = MarketEventIngestionPartition(
        source=source,
        category=category,
        market=market,
        partition_date=partition_date,
        status=status,
        event_count=event_count,
        finished_at=finished_at,
        last_error=last_error,
        retry_count=0,
    )
    db.add(row)
    return row


async def _clear_partitions_for_dates(db: AsyncSession, *partition_dates: date) -> None:
    await db.execute(
        sa.delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.partition_date.in_(partition_dates)
        )
    )
    await db.flush()


@pytest.mark.unit
def test_freshness_response_schema_shape() -> None:
    from app.schemas.market_events_freshness import (
        MarketEventsFreshnessResponse,
        MarketEventsFreshnessRow,
    )

    row = MarketEventsFreshnessRow(
        source="finnhub",
        category="earnings",
        market="us",
        window_from=date(2026, 5, 5),
        window_to=date(2026, 5, 12),
        partition_count_total=8,
        partition_count_succeeded=7,
        partition_count_failed=1,
        partition_count_running=0,
        partition_count_pending=0,
        partition_count_missing=0,
        event_count_in_window=120,
        latest_succeeded_partition_date=date(2026, 5, 11),
        latest_succeeded_finished_at=datetime(2026, 5, 12, 6, 0, tzinfo=UTC),
        hours_since_latest_succeeded=2.5,
        latest_failed_partition_date=date(2026, 5, 10),
        latest_failed_error="finnhub 429",
        expected_next_refresh_at=None,
        stale=False,
    )
    resp = MarketEventsFreshnessResponse(
        generated_at=datetime(2026, 5, 12, 8, 30, tzinfo=UTC),
        window_from=date(2026, 5, 5),
        window_to=date(2026, 5, 12),
        stale_threshold_hours=DEFAULT_STALE_THRESHOLD_HOURS,
        rows=[row],
        warnings=[],
    )
    assert resp.rows[0].source == "finnhub"
    assert resp.rows[0].latest_failed_error == "finnhub 429"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_partition_marks_day_missing(db_session: AsyncSession) -> None:
    monday = date(2026, 7, 6)
    await _clear_partitions_for_dates(db_session, monday)
    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "missing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_succeeded_marks_day_loaded(db_session: AsyncSession) -> None:
    monday = date(2026, 7, 13)
    await _clear_partitions_for_dates(db_session, monday)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    for src, cat, mkt, count in (
        ("finnhub", "earnings", "us", 12),
        ("dart", "disclosure", "kr", 4),
        ("forexfactory", "economic", "global", 3),
        ("wisefn", "earnings", "kr", 2),
    ):
        _add_partition(
            db_session,
            source=src,
            category=cat,
            market=mkt,
            partition_date=monday,
            status="succeeded",
            event_count=count,
            finished_at=fresh,
        )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "loaded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_zero_event_count_marks_day_empty(db_session: AsyncSession) -> None:
    monday = date(2026, 7, 20)
    await _clear_partitions_for_dates(db_session, monday)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    for src, cat, mkt in (
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
        ("wisefn", "earnings", "kr"),
    ):
        _add_partition(
            db_session,
            source=src,
            category=cat,
            market=mkt,
            partition_date=monday,
            status="succeeded",
            event_count=0,
            finished_at=fresh,
        )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "empty"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_failed_marks_day_error(db_session: AsyncSession) -> None:
    monday = date(2026, 7, 27)
    await _clear_partitions_for_dates(db_session, monday)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    _add_partition(
        db_session,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=monday,
        status="succeeded",
        event_count=2,
        finished_at=fresh,
    )
    _add_partition(
        db_session,
        source="dart",
        category="disclosure",
        market="kr",
        partition_date=monday,
        status="failed",
        finished_at=fresh,
        last_error="connection refused",
    )
    _add_partition(
        db_session,
        source="forexfactory",
        category="economic",
        market="global",
        partition_date=monday,
        status="succeeded",
        event_count=1,
        finished_at=fresh,
    )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_when_finished_at_older_than_window(
    db_session: AsyncSession,
) -> None:
    monday = date(2026, 8, 3)
    await _clear_partitions_for_dates(db_session, monday)
    stale = datetime.now(UTC) - timedelta(hours=STALE_AFTER_HOURS + 2)
    for src, cat, mkt in (
        ("finnhub", "earnings", "us"),
        ("dart", "disclosure", "kr"),
        ("forexfactory", "economic", "global"),
        ("wisefn", "earnings", "kr"),
    ):
        _add_partition(
            db_session,
            source=src,
            category=cat,
            market=mkt,
            partition_date=monday,
            status="succeeded",
            event_count=1,
            finished_at=stale,
        )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    states = await svc.get_per_day_states(monday, monday)
    assert states[monday] == "stale"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coverage_matrix_aggregates_by_source(db_session: AsyncSession) -> None:
    fresh = datetime.now(UTC) - timedelta(hours=1)
    monday = date(2026, 8, 10)
    tuesday = date(2026, 8, 11)
    await _clear_partitions_for_dates(db_session, monday, tuesday)
    _add_partition(
        db_session,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=monday,
        status="succeeded",
        event_count=10,
        finished_at=fresh,
    )
    _add_partition(
        db_session,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=tuesday,
        status="failed",
        last_error="429",
        finished_at=fresh,
    )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    matrix = await svc.get_coverage_matrix(monday, tuesday)

    finnhub_status = next(
        s for s in matrix.sources if s.source == "finnhub" and s.market == "us"
    )
    assert finnhub_status.succeededPartitions == 1
    assert finnhub_status.failedPartitions == 1
    assert finnhub_status.missingPartitions == 0  # both days have rows
    assert finnhub_status.eventCount == 10
    assert finnhub_status.state == "failed"
    assert finnhub_status.lastError == "429"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_empty_window_returns_empty_rows(
    db_session: AsyncSession,
) -> None:
    svc = MarketEventsFreshnessService(db_session)
    resp = await svc.compute(
        window_from=date(2100, 1, 1),
        window_to=date(2100, 1, 7),
    )
    assert resp.rows == []
    assert resp.window_from == date(2100, 1, 1)
    assert resp.window_to == date(2100, 1, 7)
    assert resp.stale_threshold_hours == DEFAULT_STALE_THRESHOLD_HOURS


@pytest.mark.asyncio
@pytest.mark.integration
async def test_freshness_aggregates_mixed_partition_states(
    db_session: AsyncSession,
) -> None:
    now = datetime(2099, 5, 12, 8, 0, tzinfo=UTC)
    db_session.add_all(
        [
            MarketEventIngestionPartition(
                source="finnhub",
                category="earnings",
                market="us",
                partition_date=date(2099, 5, 11),
                status="succeeded",
                event_count=42,
                finished_at=now - timedelta(hours=2),
            ),
            MarketEventIngestionPartition(
                source="finnhub",
                category="earnings",
                market="us",
                partition_date=date(2099, 5, 10),
                status="failed",
                event_count=0,
                last_error="finnhub 429",
            ),
            MarketEventIngestionPartition(
                source="dart",
                category="disclosure",
                market="kr",
                partition_date=date(2099, 5, 10),
                status="succeeded",
                event_count=15,
                finished_at=now - timedelta(hours=50),
            ),
        ]
    )
    await db_session.flush()

    svc = MarketEventsFreshnessService(db_session)
    resp = await svc.compute(
        window_from=date(2099, 5, 5),
        window_to=date(2099, 5, 12),
        now=now,
    )

    finnhub = next(r for r in resp.rows if r.source == "finnhub")
    dart = next(r for r in resp.rows if r.source == "dart")
    assert finnhub.partition_count_succeeded == 1
    assert finnhub.partition_count_failed == 1
    assert finnhub.hours_since_latest_succeeded == pytest.approx(2.0, abs=0.1)
    assert finnhub.stale is False
    assert finnhub.latest_failed_error == "finnhub 429"
    assert dart.stale is True
    assert any("failed partition" in warning for warning in resp.warnings)
    assert any("stale" in warning for warning in resp.warnings)
