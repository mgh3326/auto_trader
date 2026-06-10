"""news-relevance pending/ingest functional contract (ROB-491 PR2)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.news_relevance import router as news_relevance_router
from app.services import symbol_news_store
from app.services.symbol_news_store import FeedArticleInput

_HEADERS = {"X-News-Relevance-Ingest-Token": "secret"}


def _build_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(news_relevance_router)
    app.add_middleware(AuthMiddleware)

    async def _db_override() -> AsyncIterator[object]:
        yield db_session

    app.dependency_overrides[get_db] = _db_override
    return app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_then_ingest_roundtrip(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    symbol = f"S-{uuid.uuid4()}"[:20]
    url = f"https://x/rob491-r1-{uuid.uuid4()}"
    await symbol_news_store.upsert_kr_feed_articles(
        db_session,
        symbol,
        [
            FeedArticleInput(
                url=url,
                title=f"{symbol} 급락 원인",
                source="매일경제",
                published_at=datetime(2026, 6, 10, 9, tzinfo=UTC),
            )
        ],
    )
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        pending = await client.get(
            f"/trading/api/news-relevance/pending?market=kr&symbol={symbol}",
            headers=_HEADERS,
        )
        assert pending.status_code == 200
        rows = pending.json()["pending"]
        assert rows and rows[0]["url"] == url
        article_id = rows[0]["article_id"]

        resp = await client.post(
            "/trading/api/news-relevance/ingest/bulk",
            headers=_HEADERS,
            json={
                "judgments": [
                    {
                        "article_id": article_id,
                        "market": "kr",
                        "symbol": symbol,
                        "relationship": "direct",
                        "relevance": "high",
                        "price_relevance": "catalyst",
                        "score": 0.92,
                        "reason": "급락 원인 직접 보도",
                        "judged_by": "hermes",
                    },
                    {
                        "article_id": 999999999,
                        "market": "kr",
                        "symbol": symbol,
                        "relationship": "unrelated",
                        "relevance": "low",
                        "price_relevance": "none",
                        "reason": "무관",
                        "judged_by": "hermes",
                    },
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == [
        {
            "article_id": article_id,
            "market": "kr",
            "symbol": symbol,
            "status": "confirmed",
        }
    ]
    assert body["errors"] == [
        {"index": 1, "article_id": 999999999, "error": "link_not_found"}
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_invalid_enum_is_422(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "NEWS_RELEVANCE_INGEST_TOKEN", "secret", raising=False
    )
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        resp = await client.post(
            "/trading/api/news-relevance/ingest/bulk",
            headers=_HEADERS,
            json={
                "judgments": [
                    {
                        "article_id": 1,
                        "market": "kr",
                        "symbol": "x",
                        "relationship": "kinda_related",
                        "relevance": "high",
                        "price_relevance": "none",
                        "reason": "r",
                        "judged_by": "hermes",
                    }
                ]
            },
        )
    assert resp.status_code == 422  # pydantic Literal — loc에 item index 포함
