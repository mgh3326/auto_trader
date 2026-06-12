"""Endpoint test for GET /trading/api/market-events/coverage (ROB-167)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.models.market_events import MarketEventIngestionPartition
from tests.market_events_test_helpers import market_events_test_lock


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_partitions(db_session, _market_events_lock):
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
def test_coverage_endpoint_reports_succeeded_and_missing(db_session):
    """Coverage endpoint returns source states correctly."""
    monday = date(2026, 5, 11)
    fresh = datetime.now(UTC) - timedelta(hours=1)

    import asyncio

    async def _insert():
        db_session.add(
            MarketEventIngestionPartition(
                source="finnhub",
                category="earnings",
                market="us",
                partition_date=monday,
                status="succeeded",
                event_count=7,
                finished_at=fresh,
                retry_count=0,
            )
        )
        await db_session.flush()
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_insert())

    with TestClient(_app()) as client:
        res = client.get(
            "/trading/api/market-events/coverage",
            params={"from_date": monday.isoformat(), "to_date": monday.isoformat()},
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["fromDate"] == monday.isoformat()
    assert data["toDate"] == monday.isoformat()
    sources = {(s["source"], s["category"], s["market"]): s for s in data["sources"]}
    assert sources[("finnhub", "earnings", "us")]["state"] == "fresh"
    assert sources[("finnhub", "earnings", "us")]["eventCount"] == 7
    assert sources[("dart", "disclosure", "kr")]["state"] == "missing"


@pytest.mark.integration
def test_coverage_endpoint_rejects_inverted_range():
    with TestClient(_app()) as client:
        res = client.get(
            "/trading/api/market-events/coverage",
            params={"from_date": "2026-05-12", "to_date": "2026-05-11"},
        )
    assert res.status_code == 400
