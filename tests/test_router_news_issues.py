# tests/test_router_news_issues.py
"""Contract tests for the read-only /trading/api/news-issues endpoint (ROB-130)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import now_kst_naive
from app.main import app
from app.routers.dependencies import get_authenticated_user
from app.services import news_issue_clustering_service as clustering


def _mk(id: int, title: str, source: str, minutes_ago: int = 30, market: str = "us"):
    return SimpleNamespace(
        id=id,
        title=title,
        summary=None,
        source=source,
        feed_source=f"rss_{source}",
        url=f"https://example.com/{id}",
        market=market,
        keywords=[],
        article_published_at=now_kst_naive() - timedelta(minutes=minutes_ago),
        stock_symbol=None,
    )


@pytest.fixture
def client(monkeypatch):
    from app.middleware.auth import AuthMiddleware
    from app.models.trading import User

    monkeypatch.setattr(
        clustering,
        "_load_recent_articles",
        AsyncMock(
            return_value=[
                _mk(1, "Amazon raises guidance on AWS demand", "cnbc"),
                _mk(2, "AWS growth boosts Amazon outlook", "bloomberg"),
            ]
        ),
    )

    user = User(id=1, email="t@example.com", is_active=True)

    async def _load_user(request):
        return user

    monkeypatch.setattr(AuthMiddleware, "_load_user", staticmethod(_load_user))

    async def _stub_user():
        return user

    app.dependency_overrides[get_authenticated_user] = _stub_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)


@pytest.mark.unit
def test_market_issues_returns_ranked_list(client):
    resp = client.get("/trading/api/news-issues?market=us&window_hours=24&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == "us"
    assert body["window_hours"] == 24
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1
    first = body["items"][0]
    assert first["rank"] == 1
    assert "AMZN" in [rs["symbol"] for rs in first["related_symbols"]]
    assert "signals" in first
    for key in ("recency_score", "source_diversity_score", "mention_score"):
        assert 0.0 <= first["signals"][key] <= 1.0


@pytest.mark.unit
def test_market_issues_invalid_market_rejected(client):
    resp = client.get("/trading/api/news-issues?market=eu")
    assert resp.status_code == 422
