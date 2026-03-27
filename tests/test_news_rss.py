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
