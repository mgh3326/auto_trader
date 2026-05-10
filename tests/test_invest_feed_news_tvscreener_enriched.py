"""ROB-162 Task 11: feed snippet falls back to article_content for tvscreener rows.

Verifies that when a tvscreener article has article_content but no summary and no
analysis, the summarySnippet is a 240-char excerpt of article_content.

Also verifies that non-tvscreener (RSS) articles are unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_NOW = datetime(2026, 5, 10, tzinfo=UTC)

_LONG_CONTENT = (
    "The Federal Reserve held interest rates steady at its May 2026 meeting, "
    "signaling a cautious approach amid mixed economic signals. Chair Powell "
    "emphasized that the committee remains data-dependent and is watching "
    "inflation and labor market data closely. Markets reacted positively to "
    "the announcement, with the S&P 500 gaining 1.2 percent on the news. "
    "Analysts expect the Fed to begin cutting rates in the second half of 2026 "
    "if inflation continues to moderate toward the 2 percent target. This is "
    "additional text to push past the 240-character boundary for truncation."
)


def _fake_article(
    *,
    id: int,
    market: str,
    feed_source: str,
    title: str,
    url: str,
    summary: str | None = None,
    article_content: str | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = market
    a.title = title
    a.source = "Reuters"
    a.feed_source = feed_source
    a.article_published_at = _NOW
    a.stock_symbol = None
    a.stock_name = None
    a.summary = summary
    a.article_content = article_content
    a.keywords = None
    a.url = url
    return a


def _fake_relation(article_id: int, market: str, symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        article_id=article_id,
        market=market,
        symbol=symbol,
        display_name=symbol,
        source="candidate_metadata",
        matched_term=None,
        score=0.9,
        rank=1,
        raw={},
    )


def _fake_resolver():
    from app.services.invest_view_model.relation_resolver import RelationResolver

    return RelationResolver(held=set(), watch=set())


def _wire_db(rows, relations):
    """Wire a MagicMock DB with the 3 execute calls used by build_feed_news:
    1. articles query  -> scalars().all() = rows
    2. summaries query -> .all() = [] (no analysis results)
    3. related symbols -> scalars().all() = relations
    """
    db = MagicMock()

    article_result = MagicMock()
    article_result.scalars.return_value.all.return_value = list(rows)

    summary_result = MagicMock()
    summary_result.all.return_value = []

    related_result = MagicMock()
    related_result.scalars.return_value.all.return_value = list(relations)

    db.execute = AsyncMock(side_effect=[article_result, summary_result, related_result])
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tvscreener_article_with_content_has_snippet_in_feed(monkeypatch):
    """tvscreener row with article_content and no summary → summarySnippet = excerpt."""
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=1,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="Fed Holds Rates Steady",
            url="https://www.tradingview.com/news/fed-rates/",
            summary=None,
            article_content=_LONG_CONTENT,
        ),
    ]
    relations = [_fake_relation(1, "us", "AAPL")]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, relations),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
    )

    assert len(response.items) == 1
    item = response.items[0]
    assert item.feedSource == "http_tvscreener_news_us"
    assert item.summarySnippet is not None, (
        "Expected summarySnippet from article_content but got None"
    )
    # Snippet must be at most 240 visible chars (ellipsis counts as 1).
    assert len(item.summarySnippet) <= 240
    # Snippet must begin with the start of the content.
    cleaned = " ".join(_LONG_CONTENT.split())
    assert cleaned.startswith(item.summarySnippet[:50])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tvscreener_article_with_short_content_snippet_no_ellipsis(monkeypatch):
    """tvscreener row with short article_content → full content, no truncation."""
    from app.services.invest_view_model import feed_news_service as svc

    short_content = "Apple reports record quarterly revenue."
    rows = [
        _fake_article(
            id=2,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="Apple Revenue Record",
            url="https://www.tradingview.com/news/aapl-revenue/",
            summary=None,
            article_content=short_content,
        ),
    ]
    relations = [_fake_relation(2, "us", "AAPL")]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, relations),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
    )

    item = response.items[0]
    assert item.summarySnippet == short_content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rss_article_with_content_does_not_get_snippet(monkeypatch):
    """Non-tvscreener (RSS) article with article_content → summarySnippet still None."""
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=3,
            market="us",
            feed_source="rss_yahoo_finance_topstories",
            title="Yahoo Finance RSS Story",
            url="https://finance.yahoo.com/news/rss-story/",
            summary=None,
            article_content=_LONG_CONTENT,
        ),
    ]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, []),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
    )

    assert len(response.items) == 1
    item = response.items[0]
    assert item.summarySnippet is None, (
        f"RSS article should not get snippet from article_content, got: {item.summarySnippet!r}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summary_takes_precedence_over_article_content_snippet(monkeypatch):
    """When row.summary is set, it takes precedence over article_content snippet."""
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=4,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="Story with existing summary",
            url="https://www.tradingview.com/news/with-summary/",
            summary="Existing short summary.",
            article_content=_LONG_CONTENT,
        ),
    ]
    relations = [_fake_relation(4, "us", "AAPL")]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, relations),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
    )

    item = response.items[0]
    assert item.summarySnippet == "Existing short summary."
