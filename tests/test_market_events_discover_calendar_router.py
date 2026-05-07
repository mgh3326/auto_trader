"""Router test for Discover calendar endpoint (ROB-138)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
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


def _app() -> FastAPI:
    from app.core.db import AsyncSessionLocal, get_db
    from app.routers import market_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(market_events.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    async def _override_get_db():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.mark.integration
def test_discover_calendar_returns_grouped_days(db_session):
    with TestClient(_app()) as client:
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
    with TestClient(_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-10&to_date=2026-05-04&today=2026-05-07"
        )
    assert r.status_code == 400


@pytest.mark.integration
def test_discover_calendar_rejects_unknown_tab(db_session):
    with TestClient(_app()) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07&tab=bogus"
        )
    assert r.status_code == 422


@pytest.mark.integration
def test_discover_calendar_requires_auth():
    from app.routers import market_events

    app = FastAPI()
    app.include_router(market_events.router)
    with TestClient(app) as client:
        r = client.get(
            "/trading/api/market-events/discover-calendar"
            "?from_date=2026-05-04&to_date=2026-05-10&today=2026-05-07"
        )
    assert r.status_code in (401, 403)
