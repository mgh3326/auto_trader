"""ROB-117 — Candidate discovery router tests."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.candidate_discovery import get_candidate_screening_service
from app.routers.dependencies import get_authenticated_user
from app.schemas.candidate_discovery import (
    CandidateScreenResponse,
    ScreenedCandidate,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_screen_returns_payload() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()
    fake_service.screen.return_value = CandidateScreenResponse(
        generated_at="2026-05-05T00:00:00+00:00",
        market="crypto",
        strategy="oversold",
        sort_by="rsi",
        total=1,
        candidates=[
            ScreenedCandidate(symbol="KRW-ETH", name="이더리움", rsi=28.0, is_held=False)
        ],
        warnings=[],
    )

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_candidate_screening_service] = lambda: fake_service

    try:
        with patch("app.middleware.auth.AuthMiddleware._maybe_authenticate", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                res = await c.post(
                    "/trading/api/candidates/screen",
                    json={"market": "crypto", "strategy": "oversold", "limit": 10},
                )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["candidates"][0]["symbol"] == "KRW-ETH"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_screen_validates_limit_range() -> None:
    fake_user = type("U", (), {"id": 1})()
    fake_service = AsyncMock()

    app.dependency_overrides[get_authenticated_user] = lambda: fake_user
    app.dependency_overrides[get_candidate_screening_service] = lambda: fake_service

    try:
        with patch("app.middleware.auth.AuthMiddleware._maybe_authenticate", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                res = await c.post(
                    "/trading/api/candidates/screen",
                    json={"market": "crypto", "limit": 500},
                )
        assert res.status_code == 422
    finally:
        app.dependency_overrides.clear()
