"""Read-only query service tests (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_events_for_date_returns_events_with_values(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "IONQ",
            "title": "IONQ earnings release",
            "event_date": date(2026, 5, 7),
            "time_hint": "after_close",
            "status": "released",
            "source": "finnhub",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [
            {
                "metric_name": "eps",
                "period": "Q1-2026",
                "actual": Decimal("-0.38"),
                "forecast": Decimal("-0.36"),
                "unit": "USD",
            }
        ],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    response = await svc.list_for_date(date(2026, 5, 7))
    assert response.date == date(2026, 5, 7)
    assert len(response.events) == 1
    event = response.events[0]
    assert event.symbol == "IONQ"
    assert event.held is None  # placeholder until ROB-XXX follow-up
    assert event.watched is None
    assert len(event.values) == 1
    assert event.values[0].metric_name == "eps"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_events_filters_by_category_and_market(db_session):
    from app.services.market_events.query_service import MarketEventsQueryService
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "IONQ",
            "event_date": date(2026, 5, 7),
            "status": "released",
            "source": "finnhub",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await repo.upsert_event_with_values(
        {
            "category": "disclosure",
            "market": "kr",
            "symbol": "00126380",
            "event_date": date(2026, 5, 7),
            "status": "released",
            "source": "dart",
            "source_event_id": "20260507000001",
        },
        [],
    )
    await db_session.commit()

    svc = MarketEventsQueryService(db_session)
    only_kr = await svc.list_for_range(date(2026, 5, 7), date(2026, 5, 7), market="kr")
    assert only_kr.count == 1
    assert only_kr.events[0].market == "kr"

    only_earnings = await svc.list_for_range(
        date(2026, 5, 7), date(2026, 5, 7), category="earnings"
    )
    assert only_earnings.count == 1
    assert only_earnings.events[0].category == "earnings"
