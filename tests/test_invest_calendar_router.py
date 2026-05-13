"""Unit tests for calendar_service."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
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
    company_name: str | None = None,
    title: str | None = None,
    ev_date: date | None = None,
    release_time_utc: datetime | None = None,
    actual: object | None = None,
    forecast: object | None = None,
    previous: object | None = None,
    values: list[object] | None = None,
    source: str = "test",
):
    e = MagicMock()
    # MarketEventResponse uses source_event_id, not event_id
    e.source_event_id = event_id
    e.id = event_id
    e.market = market
    e.category = category
    e.symbol = symbol
    e.company_name = company_name if company_name is not None else symbol
    e.title = f"event {event_id}" if title is None else title
    e.event_date = ev_date or date(2026, 5, 4)
    e.release_time_utc = release_time_utc
    e.source = source
    if values is not None:
        e.values = values
    elif actual is not None or forecast is not None or previous is not None:
        value = MagicMock()
        value.actual = actual
        value.forecast = forecast
        value.previous = previous
        e.values = [value]
    else:
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
async def test_calendar_prioritizes_held_watchlist_and_value_events(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(event_id="ordinary", symbol="ZZZZ"),
        _fake_event(event_id="watched", symbol="MSFT"),
        _fake_event(event_id="held", symbol="AAPL"),
        _fake_event(event_id="valued", symbol="TSLA", forecast="1.23"),
    ]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)
    _patch_freshness(monkeypatch, svc, date(2026, 5, 4), date(2026, 5, 4))

    resolver = RelationResolver(
        held={("us", "AAPL")},
        watch={("us", "MSFT")},
    )
    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=resolver,
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )

    events = resp.days[0].events
    assert [event.eventId for event in events] == [
        "held",
        "watched",
        "valued",
        "ordinary",
    ]
    assert (
        events[0].displayPriority
        > events[1].displayPriority
        > events[2].displayPriority
    )
    assert events[0].highlightReasons == ["held"]
    assert events[1].highlightReasons == ["watchlist"]
    assert events[2].highlightReasons == ["has_values"]
    assert resp.days[0].summary is not None
    assert resp.days[0].summary.highlightEventIds == [
        "held",
        "watched",
        "valued",
        "ordinary",
    ]
    assert resp.days[0].summary.overflowCount == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_cluster_top_events_and_summary_are_priority_aware(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        *[
            _fake_event(
                event_id=f"ordinary-{i}",
                category="earnings",
                market="us",
                symbol=f"ZZZ{i}",
            )
            for i in range(12)
        ],
        _fake_event(
            event_id="watched", category="earnings", market="us", symbol="MSFT"
        ),
        _fake_event(event_id="held", category="earnings", market="us", symbol="AAPL"),
        _fake_event(
            event_id="valued",
            category="earnings",
            market="us",
            symbol="TSLA",
            actual="2.34",
        ),
    ]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)
    _patch_freshness(monkeypatch, svc, date(2026, 5, 4), date(2026, 5, 4))

    resolver = RelationResolver(
        held={("us", "AAPL")},
        watch={("us", "MSFT")},
    )
    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=resolver,
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )

    day = resp.days[0]
    assert len(day.clusters) == 1
    assert (
        len(day.clusters[0].topEvents) == 5
    )  # top events available for primary row rendering
    assert [event.eventId for event in day.clusters[0].topEvents[:3]] == [
        "held",
        "watched",
        "valued",
    ]
    assert day.summary is not None
    assert day.summary.highlightEventIds[:3] == ["held", "watched", "valued"]
    assert day.summary.overflowCount == 10
    assert day.summary.overflowLabel == "그 외 10개"
    assert day.summary.headline == "주요 일정 5개 · 그 외 10개"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_polishes_blank_kr_title_kst_time_and_values(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import calendar_service as svc

    value = MagicMock()
    value.actual = Decimal("1.16000000")
    value.forecast = Decimal("0.28490000")
    value.previous = Decimal("2.00000000")
    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(
            event_id="wise:005930:2026Q1",
            category="earnings",
            market="kr",
            symbol="005930",
            company_name="삼성전자",
            title="",
            release_time_utc=datetime(2026, 5, 7, 16, 30, tzinfo=UTC),
            values=[value],
        )
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

    event = resp.days[0].events[0]
    assert event.title == "삼성전자(005930) 실적 발표"
    assert event.eventTimeLocal == "5월 8일 오전 1시 30분 KST"
    assert event.actual == "1.16"
    assert event.forecast == "0.2849"
    assert event.previous == "2"


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_prefers_tradingview_over_forexfactory_duplicate(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(
            event_id="ff-cpi",
            category="economic",
            market="global",
            source="forexfactory",
            title="US CPI",
            actual=None,
            forecast="0.3",
            previous="0.2",
        ),
        _fake_event(
            event_id="tv-cpi",
            category="economic",
            market="global",
            source="tradingview",
            title="US CPI",
            actual="0.4",
            forecast="0.3",
            previous="0.2",
        ),
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
        tab="economic",
    )

    events = resp.days[0].events
    assert len(events) == 1
    assert events[0].eventId == "tv-cpi"
    assert events[0].source == "tradingview"
    assert events[0].actual == "0.4"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_keeps_forexfactory_when_tradingview_missing(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [
        _fake_event(
            event_id="ff-cpi",
            category="economic",
            market="global",
            source="forexfactory",
            title="US CPI",
            actual=None,
            forecast="0.3",
            previous="0.2",
        ),
        _fake_event(
            event_id="ff-jobs",
            category="economic",
            market="global",
            source="forexfactory",
            title="US Nonfarm Payrolls",
            actual="175",
            forecast="170",
            previous="165",
        ),
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
        tab="economic",
    )

    event_ids = [event.eventId for event in resp.days[0].events]
    assert event_ids == ["ff-cpi", "ff-jobs"]
