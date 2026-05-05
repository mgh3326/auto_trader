# tests/test_router_news_radar.py
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.schemas.news_radar import (
    NewsRadarReadiness,
    NewsRadarResponse,
    NewsRadarSummary,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.main import app
    from app.middleware.auth import AuthMiddleware
    from app.models.trading import User
    from app.routers import news_radar
    from app.routers.dependencies import get_authenticated_user

    user = User(id=1, email="op@example.test", is_active=True)

    async def fake_load_user(request):
        return user

    monkeypatch.setattr(AuthMiddleware, "_load_user", staticmethod(fake_load_user))

    async def fake_get_auth_user():
        return user

    async def fake_build(**kwargs):
        return NewsRadarResponse(
            market=kwargs.get("market", "all"),
            as_of=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
            readiness=NewsRadarReadiness(
                status="ready",
                latest_scraped_at=None,
                latest_published_at=None,
                recent_6h_count=1,
                recent_24h_count=2,
                source_count=1,
                stale=False,
                max_age_minutes=180,
            ),
            summary=NewsRadarSummary(
                high_risk_count=0,
                total_count=0,
                included_in_briefing_count=0,
                excluded_but_collected_count=0,
            ),
        )

    monkeypatch.setattr(news_radar, "build_news_radar", fake_build)
    app.dependency_overrides[get_authenticated_user] = fake_get_auth_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)


@pytest.mark.unit
def test_router_returns_radar_response(client: TestClient) -> None:
    res = client.get("/trading/api/news-radar")
    assert res.status_code == 200
    body = res.json()
    assert body["market"] == "all"
    assert body["readiness"]["status"] == "ready"


@pytest.mark.unit
def test_router_passes_filters_to_service(client: TestClient) -> None:
    res = client.get(
        "/trading/api/news-radar",
        params={
            "market": "us",
            "hours": "6",
            "q": "Iran",
            "risk_category": "geopolitical_oil",
            "include_excluded": "false",
            "limit": "20",
        },
    )
    assert res.status_code == 200
    assert res.json()["market"] == "us"


@pytest.mark.unit
def test_router_rejects_invalid_market(client: TestClient) -> None:
    res = client.get("/trading/api/news-radar", params={"market": "fx"})
    assert res.status_code == 422
