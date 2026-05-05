"""ROB-116 — Portfolio actions router tests."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.portfolio_actions import get_portfolio_action_service
from app.routers.dependencies import get_authenticated_user
from app.schemas.portfolio_actions import (
    PortfolioActionCandidate,
    PortfolioActionsResponse,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_portfolio_actions_returns_payload() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.build_action_board.return_value = PortfolioActionsResponse(
        generated_at="2026-05-05T00:00:00+00:00",
        total=1,
        candidates=[
            PortfolioActionCandidate(
                symbol="KRW-SOL",
                name="솔라나",
                market="CRYPTO",
                candidate_action="trim",
                suggested_trim_pct=20,
                reason_codes=["overweight", "research_not_bullish"],
                missing_context_codes=["journal_missing"],
            )
        ],
    )

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_portfolio_action_service] = lambda: fake_service

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            res = await c.get("/trading/api/portfolio-actions")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["candidates"][0]["candidate_action"] == "trim"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_portfolio_actions_passes_market_filter() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.build_action_board.return_value = PortfolioActionsResponse(
        generated_at="2026-05-05T00:00:00+00:00", total=0, candidates=[]
    )

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_portfolio_action_service] = lambda: fake_service

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get("/trading/api/portfolio-actions?market=CRYPTO")
        fake_service.build_action_board.assert_awaited_once_with(
            user_id=1, market_filter="CRYPTO"
        )
    finally:
        app.dependency_overrides.clear()
