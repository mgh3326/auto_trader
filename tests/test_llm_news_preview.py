from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_latest_news_preview_returns_mapped_rows():
    from app.schemas.preopen import NewsArticlePreview
    from app.services.llm_news_service import get_latest_news_preview

    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            id=1,
            title="hello",
            url="https://example.com/a",
            source="MK",
            feed_source="mk_stock",
            article_published_at=now,
            summary="s",
        ),
        SimpleNamespace(
            id=2,
            title="world",
            url="https://example.com/b",
            source=None,
            feed_source="yna_market",
            article_published_at=now - timedelta(minutes=5),
            summary=None,
        ),
    ]
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result)

    out = await get_latest_news_preview(
        db=db, feed_sources=["mk_stock", "yna_market"], limit=5
    )

    assert all(isinstance(item, NewsArticlePreview) for item in out)
    assert [item.id for item in out] == [1, 2]
    assert out[0].published_at is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_latest_news_preview_empty_when_no_feed_sources():
    from app.services.llm_news_service import get_latest_news_preview

    db = AsyncMock()
    out = await get_latest_news_preview(db=db, feed_sources=[], limit=5)
    assert out == []
    db.execute.assert_not_awaited()
