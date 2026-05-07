"""Read-only market events router (ROB-128)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
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


def _app() -> FastAPI:
    from app.core.db import get_db
    from app.routers import market_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(market_events.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.mark.integration
def test_get_today_events_returns_empty_when_no_data(db_session):
    """Smoke test: route exists, returns empty events when DB has none."""
    with TestClient(_app()) as client:
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
    from app.routers import market_events

    app = FastAPI()
    app.include_router(market_events.router)
    # Do not override get_authenticated_user — it should reject calls without a token.
    with TestClient(app) as client:
        response = client.get(
            "/trading/api/market-events/today?on_date=2026-05-07",
        )
        assert response.status_code in (401, 403)


@pytest.mark.integration
def test_get_range_events_validates_date_order(db_session):
    with TestClient(_app()) as client:
        response = client.get(
            "/trading/api/market-events/range?from_date=2026-05-08&to_date=2026-05-07",
        )
        assert response.status_code == 400
