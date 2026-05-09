"""Tests for ticker research-session news fallback (ROB-130).

Verifies that when `news_articles.stock_symbol` is null but title/summary
contain a known alias, the fallback returns those rows tagged with a match
reason — instead of an empty list.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.timezone import now_kst_naive
from app.services import llm_news_service


def _mk_article(
    *,
    id: int,
    title: str,
    summary: str | None = None,
    stock_symbol: str | None = None,
    market: str = "us",
    published_minutes_ago: int = 60,
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        title=title,
        summary=summary,
        stock_symbol=stock_symbol,
        stock_name=None,
        market=market,
        keywords=keywords or [],
        article_published_at=now_kst_naive() - timedelta(minutes=published_minutes_ago),
        url=f"https://example.com/{id}",
        source="example",
        feed_source="rss_test",
    )


def _related_symbol_session_factory(rows: list[SimpleNamespace]):
    """Return an async session factory yielding related-symbol lookup rows."""

    class _RelatedRowsSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            await asyncio.sleep(0)
            result = Mock()
            result.scalars.return_value.all.return_value = rows
            return result

    return lambda: _RelatedRowsSession()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_exact_symbol_match_returned_first(monkeypatch):
    exact = [_mk_article(id=1, title="AMZN beats", stock_symbol="AMZN")]

    def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "AMZN":
            return exact, len(exact)
        return [], 0

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    assert len(result.articles) == 1
    assert result.articles[0].id == 1
    assert result.match_reasons[1] == "exact_symbol"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_alias_used_when_exact_returns_empty(monkeypatch):
    untagged = [
        _mk_article(id=10, title="Amazon raises guidance on AWS", stock_symbol=None),
        _mk_article(id=11, title="Apple reports Q1", stock_symbol=None),
    ]

    def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "AMZN":
            return [], 0
        # market-wide query (no stock_symbol)
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )
    monkeypatch.setattr(
        llm_news_service,
        "AsyncSessionLocal",
        _related_symbol_session_factory([]),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    ids = [a.id for a in result.articles]
    assert 10 in ids
    assert 11 not in ids  # Apple article must not match AMZN
    assert result.match_reasons[10] == "alias_match"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_kr_005930_alias_match(monkeypatch):
    untagged = [
        _mk_article(
            id=20, title="삼성전자 1분기 실적 호조", stock_symbol=None, market="kr"
        )
    ]

    def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "005930":
            return [], 0
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )
    monkeypatch.setattr(
        llm_news_service,
        "AsyncSessionLocal",
        _related_symbol_session_factory([]),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="005930", market="kr", hours=24, limit=20
    )
    assert any(a.id == 20 for a in result.articles)
    assert result.match_reasons[20] == "alias_match"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_returns_empty_when_no_match(monkeypatch):
    def fake_get_news_articles(**kwargs):
        return [], 0

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )
    monkeypatch.setattr(
        llm_news_service,
        "AsyncSessionLocal",
        _related_symbol_session_factory([]),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    assert result.articles == []
    assert result.match_reasons == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_uses_persisted_related_symbol_rows_before_alias_scan(
    monkeypatch,
):
    related = _mk_article(
        id=30,
        title="Cloud capex lifts chip supply chain",
        stock_symbol=None,
        market="us",
    )
    get_news_articles = AsyncMock(side_effect=[([], 0), ([], 0)])

    class _RelatedRowsSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            await asyncio.sleep(0)
            result = Mock()
            result.scalars.return_value.all.return_value = [related]
            return result

    monkeypatch.setattr(llm_news_service, "get_news_articles", get_news_articles)
    monkeypatch.setattr(
        llm_news_service,
        "AsyncSessionLocal",
        lambda: _RelatedRowsSession(),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="NVDA", market="us", hours=24, limit=1
    )

    assert [article.id for article in result.articles] == [30]
    assert result.match_reasons[30] == "related_symbol"
    assert get_news_articles.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_supplements_exact_and_related_rows_with_alias_matches(
    monkeypatch,
):
    exact = [_mk_article(id=1, title="NVDA tagged", stock_symbol="NVDA")]
    related = _mk_article(
        id=30,
        title="Cloud capex lifts chip supply chain",
        stock_symbol=None,
        market="us",
    )
    alias = _mk_article(
        id=40,
        title="Nvidia suppliers gain on AI demand",
        stock_symbol=None,
        market="us",
    )
    unrelated = _mk_article(
        id=41,
        title="Apple reports services growth",
        stock_symbol=None,
        market="us",
    )

    def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "NVDA":
            return exact, len(exact)
        return [alias, unrelated], 2

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )
    monkeypatch.setattr(
        llm_news_service,
        "AsyncSessionLocal",
        _related_symbol_session_factory([related]),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="NVDA", market="us", hours=24, limit=20
    )

    assert [article.id for article in result.articles] == [1, 30, 40]
    assert result.match_reasons == {
        1: "exact_symbol",
        30: "related_symbol",
        40: "alias_match",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_caps_limit(monkeypatch):
    untagged = [
        _mk_article(id=i, title="Amazon news", stock_symbol=None) for i in range(50)
    ]

    def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol"):
            return [], 0
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service,
        "get_news_articles",
        AsyncMock(side_effect=fake_get_news_articles),
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=5
    )
    assert len(result.articles) == 5
