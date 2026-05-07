"""ROB-115 — NewsStageAnalyzer on-demand fetch behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analysis.stages import news_stage
from app.analysis.stages.base import StageContext
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.services.llm_news_service import NewsLookupResult
from app.services.research_news_service import NormalizedArticle


def _fake_db_article(
    *,
    title: str = "기존기사",
    published_at: datetime | None = None,
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        article_published_at=published_at,
        keywords=keywords or [],
    )


class TestNewsStageOnDemandFetch:
    @pytest.mark.asyncio
    async def test_skips_fetch_when_db_has_enough_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = [
            _fake_db_article(title="기존1"),
            _fake_db_article(title="기존2"),
            _fake_db_article(title="기존3"),
        ]
        get_articles = AsyncMock(return_value=NewsLookupResult(articles=existing))
        fetch = AsyncMock(return_value=[])
        bulk_create = AsyncMock(return_value=(0, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles_with_fallback", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="005930",
                instrument_type="equity_kr",
                symbol_name="삼성전자",
            )
        )

        assert out.signals.headline_count == 3
        fetch.assert_not_awaited()
        bulk_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_triggers_fetch_when_db_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_call_articles: list[SimpleNamespace] = []
        second_call_articles = [
            _fake_db_article(
                title="삼성전자 호실적",
                published_at=datetime(2026, 5, 5, 9, 0, 0),
                keywords=["earnings"],
            ),
        ]
        get_articles = AsyncMock(
            side_effect=[
                NewsLookupResult(articles=first_call_articles),
                NewsLookupResult(articles=second_call_articles),
            ]
        )
        fetch = AsyncMock(
            return_value=[
                NormalizedArticle(
                    url="https://finance.naver.com/x",
                    title="삼성전자 호실적",
                    source="한국경제",
                    summary=None,
                    published_at=datetime(2026, 5, 5, 9, 0, 0),
                    provider="naver",
                )
            ]
        )
        bulk_create = AsyncMock(return_value=(1, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles_with_fallback", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="005930",
                instrument_type="equity_kr",
                symbol_name="삼성전자",
            )
        )

        fetch.assert_awaited_once()
        bulk_create.assert_awaited_once()
        # bulk_create payload tags symbol/name and uses on-demand feed_source
        payload = bulk_create.await_args.args[0]
        assert payload[0].stock_symbol == "005930"
        assert payload[0].stock_name == "삼성전자"
        assert payload[0].feed_source == "research_on_demand_naver"
        assert payload[0].market == "kr"
        # signals reflect the refetched DB state
        assert out.signals.headline_count == 1

    @pytest.mark.asyncio
    async def test_fetch_failure_degrades_to_neutral(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_articles = AsyncMock(return_value=NewsLookupResult(articles=[]))
        fetch = AsyncMock(return_value=[])  # service returns [] on failure
        bulk_create = AsyncMock(return_value=(0, 0, []))
        monkeypatch.setattr(news_stage, "get_news_articles_with_fallback", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="AMZN",
                instrument_type="equity_us",
                symbol_name="Amazon.com Inc.",
            )
        )

        # No raise. Stage stays NEUTRAL with 0 headlines.
        from app.schemas.research_pipeline import StageVerdict

        assert out.verdict == StageVerdict.NEUTRAL
        assert out.signals.headline_count == 0
        # bulk_create skipped because fetched=[]
        bulk_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_fetched_headlines_when_persisted_requery_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_articles = AsyncMock(
            side_effect=[
                NewsLookupResult(articles=[]),
                NewsLookupResult(articles=[]),
            ]
        )
        fetch = AsyncMock(
            return_value=[
                NormalizedArticle(
                    url="https://reuters.com/amzn-q1",
                    title="Amazon beats Q1 earnings",
                    source="Reuters",
                    summary="Amazon reported revenue of $X.",
                    published_at=datetime(2026, 5, 5, 13, 30, 0),
                    provider="finnhub",
                )
            ]
        )
        bulk_create = AsyncMock(return_value=(0, 1, ["https://reuters.com/amzn-q1"]))
        monkeypatch.setattr(news_stage, "get_news_articles_with_fallback", get_articles)
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        monkeypatch.setattr(news_stage, "bulk_create_news_articles", bulk_create)

        analyzer = NewsStageAnalyzer()
        out = await analyzer.analyze(
            StageContext(
                session_id=1,
                symbol="AMZN",
                instrument_type="equity_us",
                symbol_name="Amazon.com Inc.",
            )
        )

        fetch.assert_awaited_once()
        bulk_create.assert_awaited_once()
        assert get_articles.await_count == 2
        assert out.signals.headline_count == 1
        assert out.signals.sentiment_score > 0
