"""ROB-75 preopen market news briefing API contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _response_kwargs(**overrides):
    defaults = {
        "has_run": True,
        "advisory_used": False,
        "advisory_skipped_reason": None,
        "run_uuid": uuid4(),
        "market_scope": "kr",
        "stage": "preopen",
        "status": "open",
        "strategy_name": "Morning scan",
        "source_profile": "roadmap",
        "generated_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "notes": None,
        "market_brief": {"summary": "cautious"},
        "source_freshness": None,
        "source_warnings": [],
        "advisory_links": [],
        "candidate_count": 0,
        "reconciliation_count": 0,
        "candidates": [],
        "reconciliations": [],
        "linked_sessions": [],
        "news": None,
        "news_preview": [],
        "news_brief": None,
    }
    defaults.update(overrides)
    return defaults


def _article(**overrides):
    published_at = overrides.pop("article_published_at", datetime.now(UTC))
    defaults = {
        "id": 10,
        "title": "코스피 장전 시황과 반도체 강세",
        "url": "https://example.com/market-open",
        "source": "Example News",
        "feed_source": "browser_naver_mainnews",
        "article_published_at": published_at,
        "scraped_at": published_at,
        "summary": "반도체와 코스피 장전 흐름 요약",
        "keywords": ["코스피", "반도체"],
        "stock_symbol": None,
        "crypto_relevance": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.unit
def test_preopen_response_serializes_market_news_briefing_contract():
    from app.schemas.preopen import (
        PreopenLatestResponse,
        PreopenMarketNewsBriefing,
        PreopenMarketNewsItem,
        PreopenMarketNewsSection,
    )

    published_at = datetime.now(UTC)
    response = PreopenLatestResponse(
        **_response_kwargs(
            market_news_briefing=PreopenMarketNewsBriefing(
                summary={
                    "included": 1,
                    "excluded": 2,
                    "sections": 1,
                    "uncategorized": 1,
                },
                sections=[
                    PreopenMarketNewsSection(
                        section_id="preopen_headlines",
                        title="장전 주요 뉴스",
                        items=[
                            PreopenMarketNewsItem(
                                id=123,
                                title="코스피 장전 시황",
                                url="https://example.com/news/123",
                                source="Example News",
                                feed_source="browser_naver_mainnews",
                                published_at=published_at,
                                summary="장전 핵심 요약",
                                briefing_relevance={
                                    "score": 64,
                                    "reason": "matched_section_terms",
                                    "section_id": "preopen_headlines",
                                    "matched_terms": ["코스피", "장전"],
                                },
                            )
                        ],
                    )
                ],
                excluded_count=2,
                top_excluded=[
                    PreopenMarketNewsItem(
                        id=124,
                        title="저신호 기사",
                        url="https://example.com/news/124",
                    )
                ],
            )
        )
    )

    dumped = response.model_dump()
    briefing = dumped["market_news_briefing"]
    assert briefing["briefing_filter"] is True
    assert briefing["summary"]["included"] == 1
    assert briefing["sections"][0]["section_id"] == "preopen_headlines"
    assert briefing["sections"][0]["items"][0]["briefing_relevance"]["score"] == 64
    assert briefing["excluded_count"] == 2
    assert briefing["top_excluded"][0]["id"] == 124


@pytest.mark.asyncio
@pytest.mark.unit
async def test_build_market_news_briefing_uses_formatter_and_maps_sections():
    from app.services import preopen_dashboard_service

    included = _article(
        id=1, title="장전 코스피 반도체 강세", keywords=["장전", "코스피", "반도체"]
    )
    excluded = _article(
        id=2, title="연예인 결혼 소식", summary="생활 문화 소식", keywords=[]
    )
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [included, excluded]
    db = AsyncMock()
    db.execute.return_value = result_mock

    briefing = await preopen_dashboard_service._build_market_news_briefing(
        db,
        market_scope="kr",
    )

    assert briefing is not None
    assert briefing.briefing_filter is True
    assert briefing.summary["included"] >= 1
    assert briefing.sections
    assert briefing.sections[0].items[0].id == 1
    assert briefing.sections[0].items[0].briefing_relevance is not None
    assert briefing.excluded_count >= 1
    assert len(briefing.top_excluded) <= 3
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_preopen_dashboard_attaches_market_news_briefing_and_preserves_news_preview():
    from app.schemas.preopen import NewsArticlePreview, PreopenMarketNewsBriefing
    from app.services import preopen_dashboard_service, research_run_service
    from tests.test_preopen_dashboard_service import _make_news_readiness, _make_run

    preview = [
        NewsArticlePreview(
            id=99,
            title="raw preview",
            url="https://example.com/raw",
            source="Example",
            feed_source="browser_naver_mainnews",
            published_at=datetime.now(UTC),
            summary=None,
        )
    ]
    market_briefing = PreopenMarketNewsBriefing(
        summary={"included": 1, "excluded": 0, "sections": 1, "uncategorized": 0},
        sections=[],
    )

    with (
        patch.object(
            research_run_service,
            "get_latest_research_run",
            new=AsyncMock(return_value=_make_run()),
        ),
        patch.object(
            preopen_dashboard_service,
            "_linked_sessions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_news_readiness",
            new=AsyncMock(return_value=_make_news_readiness()),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_latest_news_preview",
            new=AsyncMock(return_value=preview),
        ),
        patch.object(
            preopen_dashboard_service,
            "_build_market_news_briefing",
            new=AsyncMock(return_value=market_briefing),
        ),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(),
            user_id=7,
            market_scope="kr",
        )

    assert result.market_news_briefing is market_briefing
    assert result.news_preview == preview


@pytest.mark.asyncio
@pytest.mark.unit
async def test_market_news_briefing_formatter_failure_fails_open():
    from app.services import preopen_dashboard_service

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [_article()]
    db = AsyncMock()
    db.execute.return_value = result_mock

    with patch.object(
        preopen_dashboard_service,
        "format_market_news_briefing",
        side_effect=RuntimeError("formatter failed"),
    ):
        briefing = await preopen_dashboard_service._build_market_news_briefing(
            db,
            market_scope="kr",
        )

    assert briefing is None
