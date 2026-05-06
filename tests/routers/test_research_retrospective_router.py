# tests/routers/test_research_retrospective_router.py
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.routers.research_retrospective import router


@pytest.fixture
def app(db_session, user):
    _app = FastAPI()
    _app.include_router(router)
    _app.dependency_overrides[get_current_user] = lambda: user
    _app.dependency_overrides[get_db] = lambda: db_session
    return _app


@pytest.mark.asyncio
async def test_overview_empty_window_warning(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/trading/api/research-retrospective/overview?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert "no_research_summaries_in_window" in body["warnings"]
    assert body["sessions_total"] == 0


@pytest.mark.asyncio
async def test_overview_market_filter(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(
            "/trading/api/research-retrospective/overview?days=30&market=KR"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == "KR"


@pytest.mark.asyncio
async def test_overview_invalid_market_rejected(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(
            "/trading/api/research-retrospective/overview?days=30&market=BAD"
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_stage_performance_returns_array(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(
            "/trading/api/research-retrospective/stage-performance?days=30"
        )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_decisions_respects_limit(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(
            "/trading/api/research-retrospective/decisions?days=30&limit=5"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "rows" in body
    assert len(body["rows"]) <= 5


@pytest.mark.asyncio
async def test_decisions_invalid_limit_rejected(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(
            "/trading/api/research-retrospective/decisions?days=30&limit=999"
        )
    assert resp.status_code == 422
