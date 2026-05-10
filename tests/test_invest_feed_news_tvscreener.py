"""ROB-161: /invest feed surfaces tvscreener-backed news rows."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_NOW = datetime(2026, 5, 10, tzinfo=UTC)


def _fake_article(
    *,
    id: int,
    market: str,
    feed_source: str,
    title: str,
    url: str,
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
    a.summary = None
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

    # Call 1: articles
    article_result = MagicMock()
    article_result.scalars.return_value.all.return_value = list(rows)

    # Call 2: summaries (NewsAnalysisResult query returns (article_id, summary) tuples)
    summary_result = MagicMock()
    summary_result.all.return_value = []

    # Call 3: related symbols
    related_result = MagicMock()
    related_result.scalars.return_value.all.return_value = list(relations)

    db.execute = AsyncMock(
        side_effect=[article_result, summary_result, related_result]
    )
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_tab_top_includes_tvscreener_rows(monkeypatch):
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=1,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="Apple Reports Strong iPhone Sales in Asia",
            url="https://www.tradingview.com/news/AAPL/",
        ),
        _fake_article(
            id=2,
            market="kr",
            feed_source="browser_naver_mainnews",
            title="삼성전자 분기 실적",
            url="https://n.news.naver.com/x/1",
        ),
    ]
    relations = [_fake_relation(1, "us", "AAPL")]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, relations),
        resolver=_fake_resolver(),
        tab="top",
        limit=20,
        cursor=None,
    )

    feed_sources = {item.feedSource for item in response.items}
    assert "http_tvscreener_news_us" in feed_sources
    apple = next(item for item in response.items if item.id == 1)
    assert any(s.symbol == "AAPL" for s in apple.relatedSymbols)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_tab_us_filters_tvscreener_kr_rows(monkeypatch):
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=10,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="US tvscreener row",
            url="https://www.tradingview.com/news/us/",
        ),
    ]
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, [_fake_relation(10, "us", "AAPL")]),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
    )

    assert response.tab == "us"
    assert all(item.market == "us" for item in response.items)
    assert {item.feedSource for item in response.items} == {"http_tvscreener_news_us"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_tab_crypto_keeps_tvscreener_when_relevant(monkeypatch):
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=20,
            market="crypto",
            feed_source="http_tvscreener_news_crypto",
            # Title contains "bitcoin" and "surge" — both in market_price category,
            # scores >= 40 so include_in_briefing=True; row survives crypto filter.
            title="Bitcoin surges past 100K as institutional demand rises",
            url="https://www.tradingview.com/news/btc/",
        ),
    ]
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )

    response = await svc.build_feed_news(
        db=_wire_db(rows, [_fake_relation(20, "crypto", "BTCUSDT")]),
        resolver=_fake_resolver(),
        tab="crypto",
        limit=20,
        cursor=None,
    )

    assert any(
        item.feedSource == "http_tvscreener_news_crypto"
        and any(s.symbol == "BTCUSDT" for s in item.relatedSymbols)
        for item in response.items
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_include_quotes_enriches_tvscreener_related_symbols(
    monkeypatch,
):
    from app.services.invest_view_model import feed_news_service as svc

    rows = [
        _fake_article(
            id=30,
            market="us",
            feed_source="http_tvscreener_news_us",
            title="Apple guidance",
            url="https://www.tradingview.com/news/aapl-guidance/",
        ),
    ]
    relations = [_fake_relation(30, "us", "AAPL")]

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=SimpleNamespace(items=[]))
    )
    enrich_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(svc, "_enrich_related_symbols_with_quotes", enrich_mock)

    response = await svc.build_feed_news(
        db=_wire_db(rows, relations),
        resolver=_fake_resolver(),
        tab="us",
        limit=20,
        cursor=None,
        include_quotes=True,
    )

    enrich_mock.assert_awaited_once()
    enriched_items = enrich_mock.await_args.args[0]
    assert any(
        item.feedSource == "http_tvscreener_news_us" for item in enriched_items
    )
    assert response.meta.warnings == []
