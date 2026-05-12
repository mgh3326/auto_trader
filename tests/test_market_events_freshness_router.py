"""Read-only market events freshness router tests (ROB-208)."""

from __future__ import annotations

from math import isclose
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(*, authed: bool = True) -> FastAPI:
    from app.core.db import get_db
    from app.routers import market_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(market_events.router)
    if authed:
        app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)

    async def _override_get_db():
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.mark.integration
def test_get_freshness_default_window_returns_200(db_session) -> None:
    with TestClient(_app()) as client:
        response = client.get("/trading/api/market-events/freshness")
    assert response.status_code == 200
    data = response.json()
    assert "generated_at" in data
    assert "window_from" in data
    assert "window_to" in data
    assert isclose(data["stale_threshold_hours"], 30.0)
    assert isinstance(data["rows"], list)
    assert isinstance(data["warnings"], list)


@pytest.mark.integration
def test_get_freshness_unauthenticated_returns_401() -> None:
    with TestClient(_app(authed=False)) as client:
        response = client.get("/trading/api/market-events/freshness")
    assert response.status_code in (401, 403)


@pytest.mark.integration
def test_get_freshness_partial_window_returns_400(db_session) -> None:
    with TestClient(_app()) as client:
        response = client.get(
            "/trading/api/market-events/freshness?from_date=2026-05-05"
        )
    assert response.status_code == 400


@pytest.mark.integration
def test_get_freshness_rejects_inverted_window(db_session) -> None:
    with TestClient(_app()) as client:
        response = client.get(
            "/trading/api/market-events/freshness"
            "?from_date=2026-05-12&to_date=2026-05-05"
        )
    assert response.status_code == 400
