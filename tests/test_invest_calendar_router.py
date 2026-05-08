"""Unit tests for calendar_service."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_returns_per_day(monkeypatch) -> None:
    from app.services.invest_view_model import calendar_service as svc

    fake_resp = MagicMock()
    fake_resp.events = [_fake_event(event_id="e1")]
    fake_query_service = MagicMock()
    fake_query_service.list_for_range = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(svc, "MarketEventsQueryService", lambda db: fake_query_service)

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

    resp = await svc.build_calendar(
        db=MagicMock(),
        resolver=RelationResolver(),
        from_date=date(2026, 5, 4),
        to_date=date(2026, 5, 4),
        tab="all",
    )
    assert len(resp.days[0].clusters) == 1
    assert resp.days[0].clusters[0].eventCount == 15
