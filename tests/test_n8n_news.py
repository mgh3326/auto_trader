"""Tests for GET /api/n8n/news endpoint and service layer."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.schemas.n8n.news import N8nNewsItem, N8nNewsResponse, N8nNewsSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_article(
    id: int = 1,
    title: str = "테스트 뉴스 제목",
    url: str = "https://example.com/news/1",
    source: str | None = "매일경제",
    feed_source: str | None = "mk_stock",
    summary: str | None = "요약입니다",
    article_content: str | None = "본문 내용입니다" * 30,  # >300 chars
    article_published_at=None,
    keywords: list[str] | None = None,
    stock_symbol: str | None = None,
    stock_name: str | None = None,
):
    """Return a mock object that looks like a NewsArticle ORM instance."""
    from unittest.mock import MagicMock
    from datetime import datetime

    art = MagicMock()
    art.id = id
    art.title = title
    art.url = url
    art.source = source
    art.feed_source = feed_source
    art.summary = summary
    art.article_content = article_content
    art.article_published_at = article_published_at or datetime(2026, 3, 29, 8, 0)
    art.keywords = keywords or ["삼성전자", "실적"]
    art.stock_symbol = stock_symbol
    art.stock_name = stock_name
    return art


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestN8nNewsService:
    """Unit tests for n8n_news_service.fetch_n8n_news."""

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """No articles → empty items, discord_title says 0건."""
        from app.services.n8n_news_service import fetch_n8n_news

        with patch(
            "app.services.n8n_news_service.get_news_articles",
            new_callable=AsyncMock,
            return_value=([], 0),
        ):
            resp = await fetch_n8n_news(hours=2)

        assert resp.success is True
        assert resp.summary.total == 0
        assert resp.items == []
        assert "0건" in resp.discord_title

    @pytest.mark.asyncio
    async def test_articles_returned(self):
        """Articles present → items populated, discord formatting correct."""
        from app.services.n8n_news_service import fetch_n8n_news

        articles = [
            _make_fake_article(id=1, title="뉴스 A", source="매일경제"),
            _make_fake_article(id=2, title="뉴스 B", source="연합뉴스"),
        ]

        with patch(
            "app.services.n8n_news_service.get_news_articles",
            new_callable=AsyncMock,
            return_value=(articles, 2),
        ):
            resp = await fetch_n8n_news(hours=24)

        assert resp.success is True
        assert resp.summary.total == 2
        assert len(resp.items) == 2
        assert "2건" in resp.discord_title
        assert "📰" in resp.discord_title
        # discord_body should contain article titles
        assert "뉴스 A" in resp.discord_body
        assert "뉴스 B" in resp.discord_body
        # sources in summary
        assert "매일경제" in resp.summary.sources
        assert "연합뉴스" in resp.summary.sources

    @pytest.mark.asyncio
    async def test_content_preview_truncation(self):
        """content_preview is truncated to 300 chars."""
        from app.services.n8n_news_service import fetch_n8n_news

        long_content = "가" * 500
        articles = [_make_fake_article(id=1, article_content=long_content)]

        with patch(
            "app.services.n8n_news_service.get_news_articles",
            new_callable=AsyncMock,
            return_value=(articles, 1),
        ):
            resp = await fetch_n8n_news(hours=2)

        preview = resp.items[0].content_preview
        assert preview is not None
        assert len(preview) <= 303  # 300 + "..."

    @pytest.mark.asyncio
    async def test_content_preview_none_when_no_content(self):
        """content_preview is None when article_content is None."""
        from app.services.n8n_news_service import fetch_n8n_news

        articles = [_make_fake_article(id=1, article_content=None)]

        with patch(
            "app.services.n8n_news_service.get_news_articles",
            new_callable=AsyncMock,
            return_value=(articles, 1),
        ):
            resp = await fetch_n8n_news(hours=2)

        assert resp.items[0].content_preview is None

    @pytest.mark.asyncio
    async def test_filter_params_forwarded(self):
        """hours, feed_source, keyword, limit are forwarded to get_news_articles."""
        from app.services.n8n_news_service import fetch_n8n_news

        mock_get = AsyncMock(return_value=([], 0))

        with patch("app.services.n8n_news_service.get_news_articles", mock_get):
            await fetch_n8n_news(
                hours=6, feed_source="mk_stock", keyword="삼성", limit=5
            )

        mock_get.assert_called_once_with(
            hours=6,
            feed_source="mk_stock",
            keyword="삼성",
            limit=5,
        )

    @pytest.mark.asyncio
    async def test_discord_body_format(self):
        """discord_body has source prefix, URL in angle brackets, quote block."""
        from app.services.n8n_news_service import fetch_n8n_news

        articles = [
            _make_fake_article(
                id=1,
                title="테스트 기사",
                source="한경",
                url="https://example.com/1",
                summary="요약 내용",
            ),
        ]

        with patch(
            "app.services.n8n_news_service.get_news_articles",
            new_callable=AsyncMock,
            return_value=(articles, 1),
        ):
            resp = await fetch_n8n_news(hours=2)

        body = resp.discord_body
        assert "**[한경]**" in body
        assert "<https://example.com/1>" in body
        assert "> " in body  # quote block
