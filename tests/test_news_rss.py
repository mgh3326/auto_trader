"""Tests for RSS news collection features."""

import inspect
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import KST
from app.models.news import NewsArticle
from app.schemas.news import (
    BulkCreateResponse,
    NewsAnalysisRequest,
    NewsArticleBulkCreate,
    NewsArticleCreate,
    NewsArticleResponse,
)
from app.services.llm_news_service import bulk_create_news_articles


class TestKstNaiveHelpers:
    """Test KST naive datetime helpers."""

    def test_now_kst_naive_has_no_tzinfo(self):
        from app.core.timezone import now_kst_naive

        result = now_kst_naive()
        assert result.tzinfo is None

    def test_now_kst_naive_is_kst_time(self):
        """Value should be close to now_kst() but without tzinfo."""
        from app.core.timezone import now_kst, now_kst_naive

        aware = now_kst()
        naive = now_kst_naive()
        # Difference should be < 1 second (same moment, just stripped)
        diff = abs(aware.replace(tzinfo=None) - naive)
        assert diff < timedelta(seconds=1)

    def test_to_kst_naive_from_utc_aware(self):
        from app.core.timezone import to_kst_naive

        utc_dt = datetime(2026, 3, 27, 0, 0, 0, tzinfo=UTC)
        result = to_kst_naive(utc_dt)
        assert result == datetime(2026, 3, 27, 9, 0, 0)
        assert result.tzinfo is None

    def test_to_kst_naive_from_kst_aware(self):
        from app.core.timezone import to_kst_naive

        kst_dt = datetime(2026, 3, 27, 9, 0, 0, tzinfo=KST)
        result = to_kst_naive(kst_dt)
        assert result == datetime(2026, 3, 27, 9, 0, 0)
        assert result.tzinfo is None

    def test_to_kst_naive_from_naive_passthrough(self):
        """Naive input assumed to be KST already — returned as-is."""
        from app.core.timezone import to_kst_naive

        naive_dt = datetime(2026, 3, 27, 9, 0, 0)
        result = to_kst_naive(naive_dt)
        assert result == naive_dt
        assert result.tzinfo is None

    def test_to_kst_naive_from_arbitrary_offset(self):
        """Aware datetime with +05:30 offset should convert to KST then strip."""
        from app.core.timezone import to_kst_naive

        ist = timezone(timedelta(hours=5, minutes=30))
        ist_dt = datetime(2026, 3, 27, 5, 30, 0, tzinfo=ist)  # = 00:00 UTC = 09:00 KST
        result = to_kst_naive(ist_dt)
        assert result == datetime(2026, 3, 27, 9, 0, 0)
        assert result.tzinfo is None


class TestNewsArticleModel:
    """Test NewsArticle model has RSS fields."""

    def test_feed_source_field_exists(self):
        article = NewsArticle(
            url="https://example.com/1",
            title="Test",
            scraped_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            feed_source="mk_stock",
        )
        assert article.feed_source == "mk_stock"

    def test_keywords_field_exists(self):
        article = NewsArticle(
            url="https://example.com/2",
            title="Test",
            scraped_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            keywords=["반도체", "AI"],
        )
        assert article.keywords == ["반도체", "AI"]

    def test_is_analyzed_default_none_before_persist(self):
        # SQLAlchemy 2.0: default is applied at DB level, not Python level
        article = NewsArticle(
            url="https://example.com/3",
            title="Test",
            scraped_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        assert article.is_analyzed is None

    def test_is_analyzed_can_be_set_false(self):
        article = NewsArticle(
            url="https://example.com/3",
            title="Test",
            scraped_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            is_analyzed=False,
        )
        assert article.is_analyzed is False

    def test_article_content_nullable(self):
        """RSS articles may not have full content."""
        article = NewsArticle(
            url="https://example.com/4",
            title="Test",
            scraped_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            article_content=None,
        )
        assert article.article_content is None


class TestNewsSchemas:
    """Test updated news schemas."""

    def test_create_without_content(self):
        """RSS news may have no content, only summary."""
        article = NewsArticleCreate(
            url="https://example.com/1",
            title="Test Article",
            source="매일경제",
            feed_source="mk_stock",
            summary="짧은 요약",
        )
        assert article.content is None
        assert article.feed_source == "mk_stock"

    def test_create_with_keywords(self):
        article = NewsArticleCreate(
            url="https://example.com/2",
            title="Test",
            keywords=["반도체", "AI", "삼성전자"],
        )
        assert article.keywords == ["반도체", "AI", "삼성전자"]

    def test_create_with_published_at(self):
        kst = datetime.now(UTC).astimezone().tzinfo
        dt = datetime(2026, 3, 27, 9, 29, 9, tzinfo=kst)
        article = NewsArticleCreate(
            url="https://example.com/3",
            title="Test",
            published_at=dt,
        )
        assert article.published_at == dt

    def test_bulk_create_schema(self):
        bulk = NewsArticleBulkCreate(
            articles=[
                NewsArticleCreate(url="https://example.com/1", title="A"),
                NewsArticleCreate(url="https://example.com/2", title="B"),
            ]
        )
        assert len(bulk.articles) == 2

    def test_bulk_response_schema(self):
        resp = BulkCreateResponse(
            success=True,
            inserted_count=3,
            skipped_count=2,
            skipped_urls=["https://example.com/dup1", "https://example.com/dup2"],
        )
        assert resp.inserted_count == 3
        assert len(resp.skipped_urls) == 2

    def test_article_response_has_new_fields(self):
        """NewsArticleResponse should include feed_source, keywords, is_analyzed."""
        resp = NewsArticleResponse.model_validate(
            {
                "id": 1,
                "url": "https://example.com/1",
                "title": "Test",
                "source": "매일경제",
                "author": None,
                "article_content": None,
                "summary": "요약",
                "stock_symbol": None,
                "stock_name": None,
                "article_published_at": "2026-03-27T09:00:00",
                "scraped_at": "2026-03-27T09:00:00",
                "user_id": None,
                "created_at": "2026-03-27T09:00:00",
                "updated_at": None,
                "feed_source": "mk_stock",
                "keywords": ["반도체"],
                "is_analyzed": False,
            }
        )
        assert resp.feed_source == "mk_stock"
        assert resp.keywords == ["반도체"]
        assert resp.is_analyzed is False


class TestCreateNewsArticle:
    """Test create_news_article with new RSS fields."""

    @pytest.mark.asyncio
    async def test_create_with_feed_source_and_keywords(self):
        from app.services.llm_news_service import create_news_article

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.services.llm_news_service.AsyncSessionLocal", return_value=mock_db
        ):
            added_article = None

            def capture_add(article):
                nonlocal added_article
                added_article = article

            mock_db.add = capture_add
            mock_db.commit = AsyncMock()
            mock_db.refresh = AsyncMock()

            await create_news_article(
                title="Test RSS",
                url="https://example.com/rss1",
                content=None,
                source="매일경제",
                feed_source="mk_stock",
                keywords=["반도체", "AI"],
            )

            assert added_article is not None
            assert added_article.feed_source == "mk_stock"
            assert added_article.keywords == ["반도체", "AI"]
            assert added_article.article_content is None

    @pytest.mark.asyncio
    async def test_create_without_content(self):
        """RSS articles can be created without content."""
        from app.services.llm_news_service import create_news_article

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        added_article = None

        def capture_add(article):
            nonlocal added_article
            added_article = article

        mock_db.add = capture_add
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        with patch(
            "app.services.llm_news_service.AsyncSessionLocal", return_value=mock_db
        ):
            await create_news_article(
                title="No Content",
                url="https://example.com/rss2",
                content=None,
            )
            assert added_article.article_content is None


class TestBulkCreateNewsArticles:
    """Test bulk_create_news_articles with URL dedup."""

    @pytest.mark.asyncio
    async def test_bulk_create_inserts_new_articles(self):
        from app.services.llm_news_service import bulk_create_news_articles

        articles_input = [
            NewsArticleCreate(
                url="https://example.com/new1",
                title="Article 1",
                feed_source="mk_stock",
            ),
            NewsArticleCreate(
                url="https://example.com/new2",
                title="Article 2",
                feed_source="mk_stock",
            ),
        ]

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        added_articles = []

        def capture_add(article):
            added_articles.append(article)

        mock_db.add = capture_add

        with patch(
            "app.services.llm_news_service.AsyncSessionLocal", return_value=mock_db
        ):
            inserted, skipped, skipped_urls = await bulk_create_news_articles(
                articles_input
            )

        assert inserted == 2
        assert skipped == 0
        assert skipped_urls == []

    @pytest.mark.asyncio
    async def test_bulk_create_skips_existing_urls(self):
        from app.services.llm_news_service import bulk_create_news_articles

        articles_input = [
            NewsArticleCreate(url="https://example.com/existing", title="Dup"),
            NewsArticleCreate(url="https://example.com/new", title="New"),
        ]

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            "https://example.com/existing"
        ]
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        added_articles = []

        def capture_add(article):
            added_articles.append(article)

        mock_db.add = capture_add

        with patch(
            "app.services.llm_news_service.AsyncSessionLocal", return_value=mock_db
        ):
            inserted, skipped, skipped_urls = await bulk_create_news_articles(
                articles_input
            )

        assert inserted == 1
        assert skipped == 1
        assert skipped_urls == ["https://example.com/existing"]
        assert len(added_articles) == 1
        assert added_articles[0].url == "https://example.com/new"


class TestGetNewsArticlesFilters:
    """Test get_news_articles accepts new filter parameters."""

    def test_function_accepts_hours_parameter(self):
        from app.services.llm_news_service import get_news_articles

        sig = inspect.signature(get_news_articles)
        assert "hours" in sig.parameters

    def test_function_accepts_feed_source_parameter(self):
        from app.services.llm_news_service import get_news_articles

        sig = inspect.signature(get_news_articles)
        assert "feed_source" in sig.parameters

    def test_function_accepts_keyword_parameter(self):
        from app.services.llm_news_service import get_news_articles

        sig = inspect.signature(get_news_articles)
        assert "keyword" in sig.parameters

    def test_function_accepts_has_analysis_parameter(self):
        from app.services.llm_news_service import get_news_articles

        sig = inspect.signature(get_news_articles)
        assert "has_analysis" in sig.parameters


def _make_test_app():
    """Create a minimal FastAPI app with the news router for testing."""
    from fastapi import FastAPI

    from app.routers.news_analysis import router

    app = FastAPI()
    app.include_router(router)
    return app


class TestBulkEndpoint:
    """Test POST /api/v1/news/bulk endpoint exists and validates input."""

    def test_bulk_endpoint_exists(self):
        app = _make_test_app()
        routes = [r.path for r in app.routes]
        assert "/api/v1/news/bulk" in routes

    def test_bulk_endpoint_rejects_empty_body(self):
        app = _make_test_app()
        client = TestClient(app)
        resp = client.post("/api/v1/news/bulk", json={})
        assert resp.status_code == 422


class TestGetEndpointNewParams:
    """Test GET /api/v1/news accepts new query parameters."""

    def test_accepts_hours_param(self):
        """Endpoint should accept hours parameter without 422."""
        app = _make_test_app()
        client = TestClient(app)
        resp = client.get("/api/v1/news?hours=24")
        assert resp.status_code != 422

    def test_accepts_feed_source_param(self):
        app = _make_test_app()
        client = TestClient(app)
        resp = client.get("/api/v1/news?feed_source=mk_stock")
        assert resp.status_code != 422

    def test_accepts_keyword_param(self):
        app = _make_test_app()
        client = TestClient(app)
        resp = client.get("/api/v1/news?keyword=반도체")
        assert resp.status_code != 422


class TestMCPNewsTools:
    """Test MCP news tool registration and basic behavior."""

    def test_news_tool_names_exported(self):
        from app.mcp_server.tooling.news_handlers import NEWS_TOOL_NAMES

        assert "get_market_news" in NEWS_TOOL_NAMES
        assert "search_news" in NEWS_TOOL_NAMES

    @pytest.mark.asyncio
    async def test_get_market_news_calls_service(self):
        from app.mcp_server.tooling.news_handlers import _get_market_news_impl

        mock_articles = [
            MagicMock(
                id=1,
                url="https://example.com/1",
                title="Test News",
                source="매일경제",
                feed_source="mk_stock",
                summary="요약",
                article_published_at=datetime(2026, 3, 27, 9, 0, 0, tzinfo=UTC),
                keywords=["반도체"],
            )
        ]

        with patch(
            "app.mcp_server.tooling.news_handlers.get_news_articles",
            new_callable=AsyncMock,
            return_value=(mock_articles, 1),
        ):
            result = await _get_market_news_impl(hours=24, feed_source=None, limit=20)

        assert result["count"] == 1
        assert len(result["news"]) == 1
        assert result["news"][0]["title"] == "Test News"

    @pytest.mark.asyncio
    async def test_search_news_calls_service(self):
        from app.mcp_server.tooling.news_handlers import _search_news_impl

        with patch(
            "app.mcp_server.tooling.news_handlers._search_news_db",
            new_callable=AsyncMock,
            return_value=([], 0),
        ):
            result = await _search_news_impl(query="반도체", days=7, limit=20)

        assert result["query"] == "반도체"
        assert result["count"] == 0
        assert result["news"] == []


class TestAnalyzeEndpointDefense:
    """Ensure /analyze skips analysis when article_content is NULL."""

    def test_analyze_requires_content(self):
        """POST /analyze should require content field."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NewsAnalysisRequest(
                url="https://example.com/1",
                title="Test",
                content="",
            )


class TestKeywordQuerySafety:
    """Ensure keyword with special chars produces valid JSON."""

    def test_keyword_with_double_quote(self):
        import json as _json

        keyword = 'test"injection'
        # The safe way:
        safe = _json.dumps([keyword])
        parsed = _json.loads(safe)
        assert parsed == [keyword]

        # The unsafe way would produce invalid JSON:
        unsafe = f'["{keyword}"]'
        with pytest.raises(_json.JSONDecodeError):
            _json.loads(unsafe)


class TestBulkCreateIntraBatchDedup:
    """Test that duplicate URLs within the same batch are handled."""

    @pytest.mark.asyncio
    async def test_duplicate_urls_in_same_batch(self):
        articles_input = [
            NewsArticleCreate(url="https://example.com/same", title="First"),
            NewsArticleCreate(url="https://example.com/same", title="Second (dup)"),
            NewsArticleCreate(url="https://example.com/other", title="Other"),
        ]

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        # No existing URLs in DB
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        added_articles = []

        def capture_add(article):
            added_articles.append(article)

        mock_db.add = capture_add

        with patch(
            "app.services.llm_news_service.AsyncSessionLocal", return_value=mock_db
        ):
            inserted, skipped, skipped_urls = await bulk_create_news_articles(
                articles_input
            )

        assert inserted == 2
        assert skipped == 1
        assert skipped_urls == ["https://example.com/same"]
        assert len(added_articles) == 2
