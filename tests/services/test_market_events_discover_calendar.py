"""Unit tests for DiscoverCalendarService (ROB-138)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.schemas.market_events import (
    MarketEventResponse,
    MarketEventValueResponse,
    MarketEventsRangeResponse,
)
from app.services.market_events.discover_calendar import (
    DiscoverCalendarService,
    PER_DAY_VISIBLE_LIMIT,
)
from app.services.market_events.user_context import UserEventContext


def _evt(symbol, *, importance=None, category="earnings",
         d=date(2026, 5, 7), eps=None, title=None, time_hint=None,
         market="us") -> MarketEventResponse:
    values = []
    if eps is not None:
        values.append(MarketEventValueResponse(
            metric_name="eps",
            actual=eps[0],
            forecast=eps[1],
        ))
    return MarketEventResponse(
        category=category,
        market=market,
        symbol=symbol,
        event_date=d,
        source="finnhub",
        importance=importance,
        title=title,
        time_hint=time_hint,
        values=values,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_groups_events_by_date_and_marks_today():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        count=2,
        events=[
            _evt("AAPL", d=date(2026, 5, 7), title="AAPL earnings"),
            _evt("MSFT", d=date(2026, 5, 8), title="MSFT earnings"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert [d.date for d in resp.days] == [
        date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6),
        date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10),
    ]
    today_day = next(d for d in resp.days if d.date == date(2026, 5, 7))
    assert today_day.is_today is True
    assert today_day.weekday in {"목", "Thu"}
    assert len(today_day.events) == 1
    assert today_day.events[0].title.startswith("AAPL")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_held_events_render_with_held_badge_and_first():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=2,
        events=[
            _evt("OBSCURE", title="Obscure earnings"),
            _evt("AAPL", title="AAPL earnings"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    ctx = UserEventContext(frozenset({"AAPL"}), frozenset())
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=ctx,
        tab="all",
    )
    day = resp.days[0]
    assert day.events[0].badge == "보유"
    assert day.events[0].priority == "held"
    assert day.events[0].title.startswith("AAPL")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_day_visible_limit_applies_and_counts_hidden():
    events = [_evt(f"T{i}", title=f"T{i} earnings") for i in range(20)]
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=20,
        events=events,
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    day = resp.days[0]
    assert len(day.events) == PER_DAY_VISIBLE_LIMIT
    assert day.hidden_count == 20 - PER_DAY_VISIBLE_LIMIT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tab_economic_filters_to_economic_only():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=2,
        events=[
            _evt("AAPL", category="earnings"),
            _evt(None, category="economic", market="global", importance=3,
                 title="US CPI"),
        ],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="economic",
    )
    titles = [e.title for d in resp.days for e in d.events]
    assert "US CPI" in titles
    assert all("AAPL" not in (t or "") for t in titles)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subtitle_for_earnings_uses_eps_actual_and_forecast():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=1,
        events=[_evt("IONQ", title="IonQ earnings", eps=("-0.34", "-0.52"))],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    sub = resp.days[0].events[0].subtitle
    assert sub is not None
    assert "-0.34" in sub
    assert "-0.52" in sub


@pytest.mark.unit
@pytest.mark.asyncio
async def test_headline_includes_count_when_high_importance_present():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        count=1,
        events=[_evt(None, category="economic", market="global", importance=3,
                     title="US CPI")],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert resp.headline is not None and "주요" in resp.headline


@pytest.mark.unit
@pytest.mark.asyncio
async def test_week_label_uses_korean_format():
    query = AsyncMock()
    query.list_for_range = AsyncMock(return_value=MarketEventsRangeResponse(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        count=0,
        events=[],
    ))
    svc = DiscoverCalendarService(query_service=query)
    resp = await svc.build(
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 10),
        today=date(2026, 5, 7),
        ctx=UserEventContext(frozenset(), frozenset()),
        tab="all",
    )
    assert resp.week_label == "5월 1주차"
