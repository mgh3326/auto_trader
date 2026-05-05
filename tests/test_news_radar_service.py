# tests/test_news_radar_service.py
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.schemas.news import NewsReadinessResponse


@dataclass
class FakeArticle:
    id: int
    title: str
    url: str
    market: str
    feed_source: str | None = None
    source: str | None = None
    summary: str | None = None
    keywords: list[str] | None = None
    stock_symbol: str | None = None
    article_published_at: datetime | None = None
    scraped_at: datetime | None = None


def _now() -> datetime:
    return datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


def _readiness(*, ready: bool = True) -> NewsReadinessResponse:
    return NewsReadinessResponse(
        market="kr",
        is_ready=ready,
        is_stale=not ready,
        latest_run_uuid="run-1",
        latest_status="success" if ready else None,
        latest_finished_at=_now() - timedelta(minutes=5) if ready else None,
        latest_article_published_at=_now() - timedelta(minutes=10) if ready else None,
        source_counts={"rss_reuters": 5},
        source_coverage=[],
        warnings=[],
        max_age_minutes=180,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_groups_geopolitical_oil_into_section_and_marks_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import news_radar_service
    from app.services.market_news_briefing_formatter import (
        BriefingItem,
        BriefingRelevance,
        BriefingSection,
        MarketNewsBriefing,
    )

    high = FakeArticle(
        id=1,
        title="UAE airstrike on tanker in Hormuz",
        url="https://example.test/a",
        market="us",
        feed_source="rss_reuters",
        article_published_at=_now() - timedelta(hours=1),
    )
    macro = FakeArticle(
        id=2,
        title="Fed signals rate cut as CPI cools",
        url="https://example.test/b",
        market="us",
        feed_source="rss_cnbc_finance",
        article_published_at=_now() - timedelta(hours=2),
    )

    async def fake_get_news_articles(**_: Any) -> tuple[list[Any], int]:
        return ([high, macro], 2)

    async def fake_get_news_readiness(**_: Any) -> NewsReadinessResponse:
        return _readiness()

    def fake_format_market_news_briefing(articles, market, limit=None):
        if len(articles) == 1 and articles[0].id == 1:
            # High item included
            item = BriefingItem(
                article=articles[0],
                relevance=BriefingRelevance(
                    score=80,
                    section_id="geo",
                    section_title="Geo",
                    include_in_briefing=True,
                    matched_terms=["uae"],
                ),
            )
            return MarketNewsBriefing(
                market=market,
                sections=[BriefingSection(section_id="geo", title="Geo", items=[item])],
                excluded=[],
                summary={},
            )
        else:
            # Macro item excluded (low score)
            item = BriefingItem(
                article=articles[0],
                relevance=BriefingRelevance(
                    score=20,
                    section_id=None,
                    section_title=None,
                    include_in_briefing=False,
                    matched_terms=[],
                ),
            )
            return MarketNewsBriefing(
                market=market,
                sections=[],
                excluded=[item],
                summary={},
            )

    monkeypatch.setattr(news_radar_service, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(
        news_radar_service, "get_news_readiness", fake_get_news_readiness
    )
    monkeypatch.setattr(
        news_radar_service,
        "format_market_news_briefing",
        fake_format_market_news_briefing,
    )

    response = await news_radar_service.build_news_radar(
        market="all",
        hours=24,
        q=None,
        risk_category=None,
        include_excluded=True,
        limit=50,
    )

    section_ids = [s.section_id for s in response.sections]
    assert "geopolitical_oil" in section_ids
    geo_section = next(
        s for s in response.sections if s.section_id == "geopolitical_oil"
    )
    assert geo_section.severity == "high"
    assert any(item.title.startswith("UAE") for item in geo_section.items)

    assert response.summary.high_risk_count >= 1
    assert response.summary.total_count == 2
    # The macro item is below the briefing threshold so it shows up in excluded.
    assert response.summary.excluded_but_collected_count >= 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_filters_by_risk_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import news_radar_service
    from app.services.market_news_briefing_formatter import MarketNewsBriefing

    high = FakeArticle(
        id=1,
        title="Iran sanctions tighten in Hormuz",
        url="u1",
        market="us",
    )
    macro = FakeArticle(
        id=2,
        title="Fed signals rate hike",
        url="u2",
        market="us",
    )

    async def fake_get_news_articles(**_: Any) -> tuple[list[Any], int]:
        return ([high, macro], 2)

    async def fake_get_news_readiness(**_: Any) -> NewsReadinessResponse:
        return _readiness()

    def fake_format_market_news_briefing(articles, market, limit=None):
        return MarketNewsBriefing(
            market=market,
            sections=[],
            excluded=[],
            summary={},
        )

    monkeypatch.setattr(news_radar_service, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(
        news_radar_service, "get_news_readiness", fake_get_news_readiness
    )
    monkeypatch.setattr(
        news_radar_service,
        "format_market_news_briefing",
        fake_format_market_news_briefing,
    )

    response = await news_radar_service.build_news_radar(
        market="us",
        hours=24,
        q=None,
        risk_category="geopolitical_oil",
        include_excluded=False,
        limit=50,
    )

    assert all(item.risk_category == "geopolitical_oil" for item in response.items)
    assert response.excluded_items == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_returns_unavailable_readiness_when_news_pipeline_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import news_radar_service
    from app.services.market_news_briefing_formatter import MarketNewsBriefing

    async def fake_get_news_articles(**_: Any) -> tuple[list[Any], int]:
        return ([], 0)

    async def fake_get_news_readiness(**_: Any) -> NewsReadinessResponse:
        return _readiness(ready=False)

    monkeypatch.setattr(news_radar_service, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(
        news_radar_service, "get_news_readiness", fake_get_news_readiness
    )
    monkeypatch.setattr(
        news_radar_service,
        "format_market_news_briefing",
        lambda *args, **kwargs: MarketNewsBriefing("kr", [], [], {}),
    )

    response = await news_radar_service.build_news_radar(
        market="all",
        hours=24,
        q=None,
        risk_category=None,
        include_excluded=True,
        limit=50,
    )

    assert response.readiness.status in {"stale", "unavailable"}
    assert response.summary.total_count == 0
    assert response.sections == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_hides_excluded_items_when_toggle_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import news_radar_service
    from app.services.market_news_briefing_formatter import (
        BriefingItem,
        BriefingRelevance,
        MarketNewsBriefing,
    )

    article = FakeArticle(
        id=3,
        title="General market commentary without briefing score",
        url="u3",
        market="us",
    )

    async def fake_get_news_articles(**_: Any) -> tuple[list[Any], int]:
        return ([article], 1)

    async def fake_get_news_readiness(**_: Any) -> NewsReadinessResponse:
        return _readiness()

    def fake_format_market_news_briefing(articles, market, limit=None):
        item = BriefingItem(
            article=articles[0],
            relevance=BriefingRelevance(
                score=10,
                section_id=None,
                section_title=None,
                include_in_briefing=False,
                matched_terms=[],
            ),
        )
        return MarketNewsBriefing(
            market=market, sections=[], excluded=[item], summary={}
        )

    monkeypatch.setattr(news_radar_service, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(
        news_radar_service, "get_news_readiness", fake_get_news_readiness
    )
    monkeypatch.setattr(
        news_radar_service,
        "format_market_news_briefing",
        fake_format_market_news_briefing,
    )

    response = await news_radar_service.build_news_radar(
        market="us",
        hours=24,
        q=None,
        risk_category=None,
        include_excluded=False,
        limit=50,
    )

    assert response.items == []
    assert response.sections == []
    assert response.excluded_items == []
    assert response.summary.excluded_but_collected_count == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_service_aggregates_all_market_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import news_radar_service
    from app.services.market_news_briefing_formatter import MarketNewsBriefing

    seen_markets: list[str] = []

    async def fake_get_news_articles(**kwargs: Any) -> tuple[list[Any], int]:
        assert kwargs["limit"] > 50
        return ([], 0)

    async def fake_get_news_readiness(**kwargs: Any) -> NewsReadinessResponse:
        market = kwargs["market"]
        seen_markets.append(market)
        return _readiness(ready=market != "crypto").model_copy(
            update={
                "market": market,
                "source_counts": {f"rss_{market}": 1},
            }
        )

    monkeypatch.setattr(news_radar_service, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(
        news_radar_service, "get_news_readiness", fake_get_news_readiness
    )
    monkeypatch.setattr(
        news_radar_service,
        "format_market_news_briefing",
        lambda *args, **kwargs: MarketNewsBriefing("all", [], [], {}),
    )

    response = await news_radar_service.build_news_radar(
        market="all",
        hours=24,
        q=None,
        risk_category=None,
        include_excluded=True,
        limit=50,
    )

    assert seen_markets == ["kr", "us", "crypto"]
    assert response.readiness.status == "stale"
    assert response.readiness.source_count == 3
    assert "crypto: news readiness is not ready" in response.readiness.warnings
