"""Tests for RSS news collection features."""

import pytest
from datetime import datetime, UTC

from app.models.news import NewsArticle


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


from app.schemas.news import (
    NewsArticleCreate,
    NewsArticleBulkCreate,
    BulkCreateResponse,
    NewsArticleResponse,
)


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
        from datetime import datetime, timezone, timedelta

        kst = timezone(timedelta(hours=9))
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


from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta


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
            # capture the article added to db
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
        from app.schemas.news import NewsArticleCreate

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

        # Simulate no existing URLs
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        added_articles = []
        original_add = mock_db.add

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
        from app.schemas.news import NewsArticleCreate

        articles_input = [
            NewsArticleCreate(url="https://example.com/existing", title="Dup"),
            NewsArticleCreate(url="https://example.com/new", title="New"),
        ]

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        # Simulate "existing" URL already in DB
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


import inspect


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
