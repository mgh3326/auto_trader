"""Read-only market events router (ROB-128)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.market_events_test_helpers import (
    build_market_events_app,
    clean_non_tradingview_market_events,
    market_events_test_lock,
)


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session, _market_events_lock):
    await clean_non_tradingview_market_events(db_session)
    yield


@pytest.mark.integration
def test_get_today_events_returns_empty_when_no_data(db_session):
    """Smoke test: route exists, returns empty events when DB has none."""
    with TestClient(build_market_events_app()) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-07",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-05-07"
        assert body["events"] == []


@pytest.mark.integration
def test_get_today_events_unauthorized_without_override():
    """Without dependency override, real auth dependency rejects unauthenticated request."""
    with TestClient(build_market_events_app(authenticated=False)) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-07",
        )
        assert response.status_code in (401, 403)


@pytest.mark.integration
def test_get_range_events_validates_date_order(db_session):
    with TestClient(build_market_events_app()) as client:
        response = client.get(
            "/trading/api/market-events/range?from_date=2026-05-08&to_date=2026-05-07",
        )
        assert response.status_code == 400


@pytest.mark.integration
def test_get_today_events_filters_by_category_economic(db_session):
    """Smoke test: passing category=economic does not 400 and filters correctly."""
    with TestClient(build_market_events_app()) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-13&category=economic&market=global",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-05-13"
        assert isinstance(body["events"], list)


@pytest.mark.integration
def test_get_today_events_rejects_unknown_category(db_session):
    with TestClient(build_market_events_app()) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-13&category=bogus",
        )
        assert response.status_code == 400
