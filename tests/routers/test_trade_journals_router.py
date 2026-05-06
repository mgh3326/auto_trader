# tests/routers/test_trade_journals_router.py
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.routers.trade_journals import router


@pytest.fixture(autouse=True)
def mock_external_clients():
    with (
        patch(
            "app.services.merged_portfolio_service.KISClient", autospec=True
        ) as mock_kis,
        patch(
            "app.services.trade_journal_coverage_service.upbit_client", autospec=True
        ) as mock_upbit,
        patch(
            "app.services.merged_portfolio_service.get_usd_krw_rate",
            AsyncMock(return_value=1350.0),
        ),
    ):
        mock_kis.return_value.fetch_my_stocks = AsyncMock(return_value=[])
        mock_kis.return_value.fetch_my_overseas_stocks = AsyncMock(return_value=[])
        mock_upbit.fetch_my_coins = AsyncMock(return_value=[])
        mock_upbit.fetch_multiple_current_prices = AsyncMock(return_value={})
        yield mock_kis, mock_upbit


@pytest.fixture
def app(db_session, user):
    _app = FastAPI()
    _app.include_router(router)

    _app.dependency_overrides[get_current_user] = lambda: user
    _app.dependency_overrides[get_db] = lambda: db_session
    return _app


@pytest.mark.asyncio
async def test_get_coverage_returns_200(app, seed_holding_005930) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/trading/api/trade-journals/coverage")
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_get_coverage_market_filter_validation(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/trading/api/trade-journals/coverage?market=INVALID")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_journal_success(app) -> None:
    payload = {
        "symbol": "AAPL",
        "instrument_type": "equity_us",
        "thesis": "AI supercycle",
        "status": "active",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post("/api/trade-journals", json=payload)
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_create_journal_rejects_terminal_status(app) -> None:
    payload = {
        "symbol": "AAPL",
        "instrument_type": "equity_us",
        "thesis": "AI supercycle",
        "status": "closed",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.post("/trading/api/trade-journals", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_journal_success(app, seed_active_journal_005930) -> None:
    journal_id = seed_active_journal_005930.id
    payload = {"thesis": "updated thesis"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.patch(f"/trading/api/trade-journals/{journal_id}", json=payload)
    assert resp.status_code == 200
    assert resp.json()["thesis"] == "updated thesis"


@pytest.mark.asyncio
async def test_get_retrospective_returns_200(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/trading/api/trade-journals/retrospective")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_update_journal_not_found(app) -> None:
    payload = {"thesis": "updated thesis"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.patch("/trading/api/trade-journals/99999", json=payload)
    assert resp.status_code == 404
