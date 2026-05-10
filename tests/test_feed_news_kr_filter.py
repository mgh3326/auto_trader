"""ROB-169 — KR investment relevance integration tests for build_feed_news."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.news_issues import (
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

_NOW = datetime(2026, 5, 10, tzinfo=UTC)


def _kr_article(
    *,
    id: int,
    title: str,
    summary: str = "",
    keywords: list[str] | None = None,
    symbol: str | None = None,
    name: str | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.market = "kr"
    a.title = title
    a.source = "Naver"
    a.feed_source = "browser_naver_mainnews"
    a.article_published_at = _NOW
    a.stock_symbol = symbol
    a.stock_name = name
    a.summary = summary
    a.keywords = keywords or []
    a.url = f"https://example.com/kr/{id}"
    return a


def _empty_related_result() -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_society_crime_article_dropped_on_kr_tab(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=301,
            title="'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            summary="검찰은 사이코패스 평가 결과를 공개할 예정이다.",
            keywords=["사회"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_market_wide_kospi_article_kept_with_no_symbol(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=302,
            title="코스피, 외국인 매수에 2,800선 회복",
            summary="코스피가 외국인 순매수와 반도체 강세에 힘입어 2,800선을 회복했다.",
            keywords=["증시", "코스피"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [302]
    item = resp.items[0]
    assert item.relatedSymbols == []
    assert item.category == "kr_index"
    assert item.noiseReason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_society_article_suppresses_issue_chip(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    article = _kr_article(
        id=303,
        title="유명 아이돌 열애설 인정… 소속사 공식 입장",
        summary="스캔들로 번진 사생활 이슈에 팬들이 충격.",
        keywords=["연예"],
    )
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [article]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])

    issue = MarketIssue(
        id="iss-noise",
        market="kr",
        rank=1,
        issue_title="연예 이슈",
        subtitle=None,
        direction="neutral",
        source_count=1,
        article_count=1,
        updated_at=_NOW,
        articles=[
            MarketIssueArticle(
                id=303,
                title=article.title,
                url=article.url,
                source="Naver",
                feed_source="browser_naver_mainnews",
                published_at=_NOW,
            )
        ],
        signals=IssueSignals(
            recency_score=0.5, source_diversity_score=0.5, mention_score=0.5
        ),
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    # On the "top" tab the row is NOT dropped (only the kr tab applies the
    # filter), but the issueId must be suppressed because the row is noise.
    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [303]
    assert resp.items[0].issueId is None
    assert resp.items[0].noiseReason == "kr_society"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_top_tab_does_not_drop_kr_society_rows(monkeypatch) -> None:
    """The kr-tab filter only fires on tab=='kr'; other tabs keep the row but
    still flag noiseReason so the frontend can choose to render or hide it.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=304,
            title="유명 아이돌 열애설 인정… 소속사 공식 입장",
            summary="연예 가십.",
            keywords=["연예"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [304]
    assert resp.items[0].noiseReason == "kr_society"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_symbol_market_wide_row_advertises_kr_market_wide_scope(monkeypatch) -> None:
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _kr_article(
            id=305,
            title="원달러 환율 1,400원 돌파… 수출주 영향",
            summary="달러원 환율이 1,400원을 돌파했다.",
            keywords=["환율"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[scalar_result, summary_result, _empty_related_result()])
    monkeypatch.setattr(svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[])))

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="kr", limit=30, cursor=None
    )

    assert resp.items[0].scope == "kr_market_wide"
    assert resp.items[0].relatedSymbols == []
