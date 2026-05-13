"""Tests for feed_news_service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
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
    title: str | None = None,
    summary: str | None = None,
    keywords: list[str] | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = market
    a.title = title or f"news {id}"
    a.source = "Reuters"
    a.feed_source = "rss_test"
    a.article_published_at = published_at or _NOW
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = summary or "snippet"
    a.keywords = keywords
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


def _empty_related_result() -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr", symbol="005930", name="삼성전자"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

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
async def test_feed_news_topic_filter_keeps_fx_items(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=101,
            market="kr",
            symbol="005930",
            name="삼성전자",
            title="원달러 환율 상승에 수출주 강세",
            keywords=["환율", "달러"],
        ),
        _fake_article(
            id=102,
            market="kr",
            symbol="000660",
            name="SK하이닉스",
            title="국채 금리 하락에 성장주 반등",
            keywords=["금리", "국채"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="top", limit=30, cursor=None, topic="fx"
    )

    assert [item.id for item in resp.items] == [101]
    assert resp.items[0].topicTags == ["fx"]
    assert "fx" in resp.items[0].tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_holdings_empty_when_no_holdings(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = []
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
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
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver(held={("us", "AAPL")})
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="holdings", limit=30, cursor=None
    )
    assert resp.items[0].relation == "held"
    assert resp.items[0].relatedSymbols[0].relation == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_matches_related_symbols_from_article_alias(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=20,
            market="kr",
            symbol=None,
            name=None,
            title="삼성전자 반도체 실적 기대감 확대",
            keywords=["반도체", "삼전"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver(watch={("kr", "005930")})
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="latest", limit=30, cursor=None
    )

    assert resp.items[0].relation == "watchlist"
    assert [(s.market, s.symbol) for s in resp.items[0].relatedSymbols] == [
        ("kr", "005930")
    ]
    assert resp.items[0].relatedSymbols[0].displayName == "삼성전자"
    assert resp.items[0].relatedSymbols[0].relation == "watchlist"
    assert resp.items[0].relatedSymbols[0].matchReason == "alias_dict"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_dedupes_stock_symbol_and_alias_match(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=21,
            market="us",
            symbol="AAPL",
            name="Apple Inc.",
            title="Apple shares rise after iPhone update",
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver(held={("us", "AAPL")})
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="latest", limit=30, cursor=None
    )

    assert resp.items[0].relation == "held"
    assert [(s.market, s.symbol) for s in resp.items[0].relatedSymbols] == [
        ("us", "AAPL")
    ]
    assert resp.items[0].relatedSymbols[0].displayName == "Apple Inc."
    assert resp.items[0].relatedSymbols[0].matchReason == "stock_symbol"


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
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

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
        _fake_article(id=99, market="kr", symbol="005930", name="삼성전자"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="top", limit=30, cursor=None
    )
    assert resp.items[0].issueId is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_drops_low_relevance_no_symbol(monkeypatch) -> None:
    """ROB-188: a KR article with no related symbols and no market-wide scope
    must NOT appear on the Toss-style default tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=801,
            market="kr",
            symbol=None,
            title="서울 아파트 청약 경쟁률 다시 상승",
            summary="분양 시장 실수요자 관심이 높아졌다는 분석입니다.",
            keywords=["청약", "분양"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_keeps_kr_market_wide(monkeypatch) -> None:
    """ROB-188: a KR macro/index article scored kr_market_wide (no symbol but
    investment-relevant) must remain visible on tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=802,
            market="kr",
            symbol=None,
            title="코스피, 환율 안정에 2,800선 회복",
            summary="증시가 외국인 순매수에 힘입어 상승 마감.",
            keywords=["코스피", "환율", "증시"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [802]
    assert resp.items[0].scope == "kr_market_wide"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_keeps_symbol_anchored(monkeypatch) -> None:
    """ROB-188: a US article with an anchored symbol must stay on tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=803, market="us", symbol="NVDA", name="NVIDIA"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [803]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_hot_tab_drops_low_relevance_no_symbol(monkeypatch) -> None:
    """ROB-188: tab=hot uses the same Toss-style no-symbol filter as top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=804,
            market="kr",
            symbol=None,
            title="보험료 환급 꿀팁 모음",
            summary="숨은 보험료를 확인하는 방법을 안내합니다.",
            keywords=["보험료", "환급"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="hot", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_issue_chip_suppressed_when_no_related_symbols(
    monkeypatch,
) -> None:
    """ROB-188: an item with no relatedSymbols should NOT carry an issueId
    (the chip would be unanchored and just repeat headline noise)."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=901,
            market="kr",
            symbol=None,
            title="코스피, 환율 안정에 2,800선 회복",
            summary="증시가 외국인 순매수에 힘입어 상승 마감.",
            keywords=["코스피", "환율", "증시"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

    issue = _fake_issue(issue_id="iss-901", article_ids=[901], market="kr")
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert resp.items[0].issueId is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_issue_chip_suppressed_when_title_duplicates(
    monkeypatch,
) -> None:
    """ROB-188: if the issue's issue_title exactly matches the article title
    (after trim), suppress the chip — it would just repeat the headline."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=902,
            market="us",
            symbol="AAPL",
            name="Apple",
            title="Apple shares rise after iPhone update",
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

    issue = MarketIssue(
        id="iss-902",
        market="us",
        rank=1,
        issue_title="Apple shares rise after iPhone update",
        subtitle=None,
        direction="neutral",
        source_count=1,
        article_count=1,
        updated_at=_NOW,
        articles=[
            MarketIssueArticle(
                id=902,
                title="Apple shares rise after iPhone update",
                url="https://example.com/902",
                source="Reuters",
                feed_source="rss_test",
                published_at=_NOW,
            )
        ],
        signals=IssueSignals(
            recency_score=0.5,
            source_diversity_score=0.5,
            mention_score=0.5,
        ),
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="latest", limit=30, cursor=None
    )

    assert resp.items[0].issueId is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_prefers_persisted_related_symbols(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=201,
            market="kr",
            symbol="005930",
            name="삼성전자 legacy",
            title="회사명 없는 후보 기반 기사",
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    related_result = MagicMock()
    related_result.scalars.return_value.all.return_value = [
        SimpleNamespace(
            article_id=201,
            market="kr",
            symbol="005930",
            display_name="삼성전자 candidate",
            source="candidate_metadata",
            matched_term="삼전",
        )
    ]
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, related_result])
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db,
        resolver=RelationResolver(held={("kr", "005930")}),
        tab="latest",
        limit=30,
        cursor=None,
    )

    assert [(s.market, s.symbol) for s in resp.items[0].relatedSymbols] == [
        ("kr", "005930")
    ]
    related = resp.items[0].relatedSymbols[0]
    assert related.displayName == "삼성전자 candidate"
    assert related.matchReason == "candidate_metadata"
    assert related.matchedTerm == "삼전"
    assert related.relation == "held"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_url_metadata_false_positive_stays_suppressed(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=202,
            market="kr",
            symbol=None,
            title="마켓레이더 오전 자료",
            summary="증권사 시장 요약",
            keywords=["canonical_url:https://finance.naver.com/market_info_read.naver"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert resp.items[0].relation == "none"
    assert resp.items[0].relatedSymbols == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_malformed_bracket_token_does_not_crash_matching(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=204,
            market="kr",
            symbol=None,
            title="삼성전자 [malformed",
            summary="증권사 시장 요약 foo[bar.com",
            keywords=["[broken", "canonical_url:https://finance.naver.com/[bad"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db,
        resolver=RelationResolver(watch={("kr", "005930")}),
        tab="kr",
        limit=30,
        cursor=None,
    )

    assert [(s.market, s.symbol) for s in resp.items[0].relatedSymbols] == [
        ("kr", "005930")
    ]
    assert resp.items[0].relatedSymbols[0].relation == "watchlist"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_default_does_not_call_quote_provider(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=203, market="us", symbol="AAPL", name="Apple"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )
    quote_mock = AsyncMock()
    monkeypatch.setattr(svc, "get_quote", quote_mock)

    await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="latest", limit=30, cursor=None
    )

    quote_mock.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_include_quotes_dedupes_and_computes_change(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=204, market="us", symbol="AAPL", name="Apple"),
        _fake_article(id=205, market="us", symbol="AAPL", name="Apple"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )
    quote_mock = AsyncMock(
        return_value=SimpleNamespace(
            price=110.0,
            previous_close=100.0,
            source="test-provider",
        )
    )
    monkeypatch.setattr(svc, "get_quote", quote_mock)

    resp = await svc.build_feed_news(
        db=db,
        resolver=RelationResolver(),
        tab="latest",
        limit=30,
        cursor=None,
        include_quotes=True,
    )

    quote_mock.assert_awaited_once_with(symbol="AAPL", market="us")
    quoted = resp.items[0].relatedSymbols[0]
    assert quoted.currentPrice == pytest.approx(110.0)
    assert quoted.previousClose == pytest.approx(100.0)
    assert quoted.change == pytest.approx(10.0)
    assert quoted.changePct == pytest.approx(10.0)
    assert quoted.quoteSource == "test-provider"
    assert quoted.quoteAsOf is not None
    assert resp.meta.warnings == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_include_quotes_handles_missing_previous_close(
    monkeypatch,
) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=206, market="kr", symbol="005930", name="삼성전자"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )
    monkeypatch.setattr(
        svc,
        "get_quote",
        AsyncMock(
            return_value=SimpleNamespace(
                price=70000.0, previous_close=None, source="kis"
            )
        ),
    )

    resp = await svc.build_feed_news(
        db=db,
        resolver=RelationResolver(),
        tab="kr",
        limit=30,
        cursor=None,
        include_quotes=True,
    )

    quoted = resp.items[0].relatedSymbols[0]
    assert quoted.currentPrice == pytest.approx(70000.0)
    assert quoted.previousClose is None
    assert quoted.change is None
    assert quoted.changePct is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_include_quotes_provider_failure_is_non_fatal(
    monkeypatch,
) -> None:
    from app.services.domain_errors import UpstreamUnavailableError
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=207, market="us", symbol="MSFT", name="Microsoft"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )
    monkeypatch.setattr(
        svc,
        "get_quote",
        AsyncMock(side_effect=UpstreamUnavailableError("provider down")),
    )

    resp = await svc.build_feed_news(
        db=db,
        resolver=RelationResolver(),
        tab="latest",
        limit=30,
        cursor=None,
        include_quotes=True,
    )

    assert len(resp.items) == 1
    assert resp.items[0].relatedSymbols[0].currentPrice is None
    assert "quote_unavailable:us:MSFT" in resp.meta.warnings
    assert "quote_partial_failure:1" in resp.meta.warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_kr_society_crime_dropped_on_kr_tab(monkeypatch) -> None:
    """ROB-169 regression: known-bad production row must not appear on tab=kr."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=999,
            market="kr",
            symbol=None,
            title="'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            summary="검찰은 사이코패스 평가 결과를 공개할 예정이다.",
            keywords=["사회"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_kr_article_with_us_alias_emits_us_related_symbol(
    monkeypatch,
) -> None:
    """ROB-172: a KR-feed article that names a US-aliased entity (엔비디아 →
    NVDA) must surface the US asset in relatedSymbols. Source market stays
    "kr" (it is the article's feed market), asset market is "us".
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=9659,
            market="kr",
            symbol=None,
            name=None,
            title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
            summary="엔비디아의 차세대 GPU 발표가 국내 반도체 공급망에 호재로 작용",
            keywords=["엔비디아", "반도체"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="latest", limit=30, cursor=None
    )

    item = resp.items[0]
    assert item.market == "kr"  # source/feed market preserved
    assert item.sourceMarket == "kr"  # additive contract field
    related_pairs = [(s.market, s.symbol) for s in item.relatedSymbols]
    assert ("us", "NVDA") in related_pairs
    nvda = next(s for s in item.relatedSymbols if s.symbol == "NVDA")
    assert nvda.market == "us"  # asset market, not source market
    assert nvda.matchReason == "alias_dict"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_us_article_alias_match_unchanged_after_cross_market(
    monkeypatch,
) -> None:
    """ROB-172 regression-guard: widening the matcher to ALL_ALIASES must not
    change the relatedSymbols answer for an obviously US-anchored article.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=9700,
            market="us",
            symbol=None,
            name=None,
            title="Amazon raises guidance on AWS demand",
            summary="Amazon Web Services revenue beat expectations.",
            keywords=["AWS", "Amazon"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="us", limit=30, cursor=None
    )

    item = resp.items[0]
    assert item.market == "us"
    assert item.sourceMarket == "us"
    related_pairs = [(s.market, s.symbol) for s in item.relatedSymbols]
    assert ("us", "AMZN") in related_pairs
    # No KR/crypto false-positive should appear on a clean US headline.
    assert all(s.market == "us" for s in item.relatedSymbols), related_pairs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_emits_source_market_alongside_legacy_market(
    monkeypatch,
) -> None:
    """ROB-172 dual-emission contract: every FeedNewsItem returns both `market`
    and `sourceMarket`, equal in value, so the frontend can migrate readers
    incrementally without coordinated deploy.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr"),
        _fake_article(id=2, market="us", symbol="AAPL", name="Apple"),
        _fake_article(id=3, market="crypto", symbol="BTC", name="Bitcoin"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="latest", limit=30, cursor=None
    )

    assert len(resp.items) == 3
    for item in resp.items:
        assert item.sourceMarket == item.market
        assert item.sourceMarket in ("kr", "us", "crypto")
