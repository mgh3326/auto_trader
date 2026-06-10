"""news-relevance ingest token gate (ROB-491 PR2) — 403/401 contract."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.news_relevance import router as news_relevance_router

_PATH = "/trading/api/news-relevance/ingest/bulk"
_BODY = {
    "judgments": [
        {
            "article_id": 1,
            "market": "kr",
            "symbol": "035420",
            "relationship": "direct",
            "relevance": "high",
            "price_relevance": "catalyst",
            "reason": "직접 관련",
            "judged_by": "hermes",
        }
    ]
}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(news_relevance_router)
    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfigured_token_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "NEWS_RELEVANCE_INGEST_TOKEN", "", raising=False)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(_PATH, json=_BODY)
    assert resp.status_code == 403
    assert "not configured" in cast(str, resp.json()["detail"]).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            _PATH, json=_BODY, headers={"X-News-Relevance-Ingest-Token": "nope"}
        )
    assert resp.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_get_also_token_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.get("/trading/api/news-relevance/pending?market=kr")
    assert resp.status_code == 401
