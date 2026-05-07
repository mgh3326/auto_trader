"""Tests for feed_news_service."""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.invest_view_model.relation_resolver import RelationResolver


def _fake_article(*, id: int, market: str = "kr", symbol: str | None = None,
                  name: str | None = None, published_at: datetime | None = None) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = market
    a.title = f"news {id}"
    a.source = "Reuters"
    a.feed_source = "rss_test"
    a.article_published_at = published_at or datetime(2026, 5, 1, tzinfo=timezone.utc)
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = "snippet"
    a.url = f"https://example.com/{id}"
    return a


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

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(db=db, resolver=resolver, tab="top", limit=30, cursor=None)
    assert resp.tab == "top"
    assert len(resp.items) == 1
    assert resp.items[0].id == 1
    assert resp.items[0].relation == "none"


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
