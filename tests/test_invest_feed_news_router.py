"""Tests for feed_news_service."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.news_issues import (
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssueMarket,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


def _fake_article(
    *,
    id: int,
    market: str = "kr",
    symbol: str | None = None,
    name: str | None = None,
    published_at: datetime | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = market
    a.title = f"news {id}"
    a.source = "Reuters"
    a.feed_source = "rss_test"
    a.article_published_at = published_at or _NOW
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = "snippet"
    a.url = f"https://example.com/{id}"
    return a


def _fake_issue(
    *, issue_id: str, article_ids: list[int], market: MarketIssueMarket = "kr"
) -> MarketIssue:
    articles = [
        MarketIssueArticle(
            id=aid,
            title=f"article {aid}",
            url=f"https://example.com/{aid}",
            source="Reuters",
            feed_source="rss_test",
            published_at=_NOW,
        )
        for aid in article_ids
    ]
    return MarketIssue(
        id=issue_id,
        market=market,
        rank=1,
        issue_title=f"Issue {issue_id}",
        subtitle=None,
        direction="neutral",
        source_count=1,
        article_count=len(article_ids),
        updated_at=_NOW,
        articles=articles,
        signals=IssueSignals(
            recency_score=0.5,
            source_diversity_score=0.5,
            mention_score=0.5,
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

    issue = _fake_issue(issue_id="iss-1", article_ids=[1], market="kr")
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="top", limit=30, cursor=None
    )
    assert resp.tab == "top"
    assert len(resp.items) == 1
    assert resp.items[0].id == 1
    assert resp.items[0].issueId == "iss-1"
    assert resp.items[0].relation == "none"
    assert [i.id for i in resp.issues] == ["iss-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_holdings_empty_when_no_holdings(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = []
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="holdings", limit=30, cursor=None
    )
    assert resp.meta.emptyReason == "no_holdings"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_assigns_held_relation(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=10, market="us", symbol="AAPL", name="Apple"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver(held={("us", "AAPL")})
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="holdings", limit=30, cursor=None
    )
    assert resp.items[0].relation == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_latest_tab_links_issue(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=42, market="us", symbol="AAPL", name="Apple"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

    issue = _fake_issue(issue_id="iss-42", article_ids=[42], market="us")
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="latest", limit=30, cursor=None
    )
    assert resp.items[0].issueId == "iss-42"
    assert [i.id for i in resp.issues] == ["iss-42"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_no_issue_means_none(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=99, market="kr"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="top", limit=30, cursor=None
    )
    assert resp.items[0].issueId is None
