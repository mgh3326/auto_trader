"""Router test for Discover calendar endpoint (ROB-138)."""

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
async def _clean(db_session, _market_events_lock):
    await clean_non_tradingview_market_events(db_session)
    yield


@pytest.mark.integration
def test_discover_calendar_returns_grouped_days(db_session):
    with TestClient(build_market_events_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07&tab=all"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["from_date"] == "2026-05-04"
    assert body["to_date"] == "2026-05-10"
    assert body["today"] == "2026-05-07"
    assert body["tab"] == "all"
    assert body["week_label"].endswith("주차")
    assert isinstance(body["days"], list) and len(body["days"]) == 7
    today_day = next(d for d in body["days"] if d["date"] == "2026-05-07")
    assert today_day["is_today"] is True


@pytest.mark.integration
def test_discover_calendar_validates_date_order(db_session):
    with TestClient(build_market_events_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-10&to_date=2026-05-04&today=2026-05-07"
        )
    assert r.status_code == 400


@pytest.mark.integration
def test_discover_calendar_rejects_unknown_tab(db_session):
    with TestClient(build_market_events_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07&tab=bogus"
        )
    assert r.status_code == 422


@pytest.mark.integration
def test_discover_calendar_requires_auth():
    with TestClient(build_market_events_app(authenticated=False)) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07"
        )
    assert r.status_code in (401, 403)
