"""Unit tests for calendar_service."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.calendar_freshness import CalendarCoverage, CoverageMatrixResponse
from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_event(
    *,
    event_id: str,
    market: str = "us",
    category: str = "earnings",
    symbol: str | None = None,
    ev_date: date | None = None,
):
    e = MagicMock()
    # MarketEventResponse uses source_event_id, not event_id
    e.source_event_id = event_id
    e.id = event_id
    e.market = market
    e.category = category
    e.symbol = symbol
    e.company_name = symbol
    e.title = f"event {event_id}"
    e.event_date = ev_date or date(2026, 5, 4)
    e.release_time_utc = None
    e.source = "test"
    e.values = []
    return e


def _empty_coverage(from_date: date, to_date: date) -> CoverageMatrixResponse:
    return CoverageMatrixResponse(
        fromDate=from_date,
        toDate=to_date,
        asOf=datetime.now(UTC),
        sources=[],
        partitions=[],
        coverage=CalendarCoverage(
            fromDate=from_date,
            toDate=to_date,
            expectedPartitions=0,
            succeededPartitions=0,
            failedPartitions=0,
            missingPartitions=0,
            totalEvents=0,
        ),
    )


def _patch_freshness(monkeypatch, svc, from_date: date, to_date: date, states=None):
    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(return_value=states or {})
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=_empty_coverage(from_date, to_date)
    )
    monkeypatch.setattr(svc, "MarketEventsFreshnessService", lambda db: fake_freshness)
    return fake_freshness


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_returns_per_day(monkeypatch) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [_fake_event(event_id="e1")]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)
    _patch_freshness(monkeypatch, svc, date(2026, 5, 4), date(2026, 5, 4))

    db = MagicMock()
    resolver = RelationResolver()
    resp = await svc.build_calendar(
        db=db,
        resolver=resolver,
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )
    assert len(resp.days) == 1
    assert len(resp.days[0].events) == 1
    assert resp.days[0].events[0].eventId == "e1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_clusters_when_over_threshold(monkeypatch) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(event_id=f"e{i}", category="earnings", market="us")
        for i in range(15)
    ]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)
    _patch_freshness(monkeypatch, svc, date(2026, 5, 4), date(2026, 5, 4))

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )
    assert len(resp.days[0].clusters) == 1
    assert resp.days[0].clusters[0].eventCount == 15


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_calendar_returns_one_day_per_date_for_month_range(
    monkeypatch,
) -> None:
    """ROB-165: month-range request returns N days, one CalendarDay per date."""
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(event_id=f"e{i}", ev_date=date(2026, 5, i + 1)) for i in range(31)
    ]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)
    _patch_freshness(monkeypatch, svc, date(2026, 5, 1), date(2026, 5, 31))

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 1),
        to_date=date(2026, 5, 31),
        tab="all",
    )
    assert resp.fromDate == date(2026, 5, 1)
    assert resp.toDate == date(2026, 5, 31)
    assert len(resp.days) == 31
    dates = [d.date for d in resp.days]
    assert dates == sorted(dates)
    assert len(set(dates)) == 31


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_meta_includes_source_freshness(monkeypatch) -> None:
    from app.schemas.calendar_freshness import (
        CalendarCoverage,
        CalendarSourceStatus,
        CoverageMatrixResponse,
    )
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = []
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(
        return_value={date(2026, 5, 11): "missing"}
    )
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=CoverageMatrixResponse(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            asOf=datetime.now(UTC),
            sources=[
                CalendarSourceStatus(
                    source="finnhub",
                    category="earnings",
                    market="us",
                    state="missing",
                )
            ],
            partitions=[],
            coverage=CalendarCoverage(
                fromDate=date(2026, 5, 11),
                toDate=date(2026, 5, 11),
                expectedPartitions=3,
                succeededPartitions=0,
                failedPartitions=0,
                missingPartitions=3,
                totalEvents=0,
            ),
        )
    )
    monkeypatch.setattr(svc, "MarketEventsFreshnessService", lambda db: fake_freshness)

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 11),
        tab="all",
    )
    assert resp.days[0].dataState == "missing"
    assert len(resp.meta.sourceFreshness) == 1
    assert resp.meta.sourceFreshness[0].state == "missing"
    assert resp.meta.coverage is not None
    assert resp.meta.coverage.missingPartitions == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_marks_loaded_when_events_present(monkeypatch) -> None:
    from app.schemas.calendar_freshness import (
        CalendarCoverage,
        CoverageMatrixResponse,
    )
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [_fake_event(event_id="e1", ev_date=date(2026, 5, 11))]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

    fake_freshness = MagicMock()
    fake_freshness.get_per_day_states = AsyncMock(
        return_value={date(2026, 5, 11): "loaded"}
    )
    fake_freshness.get_coverage_matrix = AsyncMock(
        return_value=CoverageMatrixResponse(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            asOf=datetime.now(UTC),
            sources=[],
            partitions=[],
            coverage=CalendarCoverage(
                fromDate=date(2026, 5, 11),
                toDate=date(2026, 5, 11),
                expectedPartitions=3,
                succeededPartitions=3,
                failedPartitions=0,
                missingPartitions=0,
                totalEvents=1,
            ),
        )
    )
    monkeypatch.setattr(svc, "MarketEventsFreshnessService", lambda db: fake_freshness)

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 11),
        to_date=date(2026, 5, 11),
        tab="all",
    )
    assert resp.days[0].dataState == "loaded"
