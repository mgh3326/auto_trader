"""ROB-424 — NewsStageAnalyzer on-demand-first behavior (get_news canonical)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.analysis.stages import news_stage
from app.analysis.stages.base import StageContext
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.schemas.research_pipeline import StageVerdict
from app.services.symbol_news_service import SymbolNewsArticle, SymbolNewsFetchResult


def _article(*, title: str, published_at: datetime | None = None) -> SymbolNewsArticle:
    return SymbolNewsArticle(
        provider="finnhub",
        market="us",
        symbol="AMZN",
        external_article_id=None,
        title=title,
        source_name="Reuters",
        canonical_url="https://example.com/x",
        summary=None,
        published_at=published_at,
        fetched_at=datetime(2026, 5, 5, 13, 30, tzinfo=UTC),
    )


def _result(status: str, articles: list[SymbolNewsArticle]) -> SymbolNewsFetchResult:
    return SymbolNewsFetchResult(
        symbol="AMZN",
        market="us",
        provider="finnhub",
        status=status,
        requested_limit=20,
        returned_count=len(articles),
        articles=articles,
    )


def _ctx(symbol: str = "AMZN", instrument_type: str = "equity_us") -> StageContext:
    return StageContext(
        session_id=1,
        symbol=symbol,
        instrument_type=instrument_type,
        symbol_name="Amazon.com Inc.",
    )


class TestNewsStageOnDemandFirst:
    @pytest.mark.asyncio
    async def test_no_broad_db_seam_on_module(self) -> None:
        # ROB-424 AC1: the broad-DB helpers must no longer be imported here.
        assert not hasattr(news_stage, "get_news_articles_with_fallback")
        assert not hasattr(news_stage, "bulk_create_news_articles")

    @pytest.mark.asyncio
    async def test_ok_positive_headline_is_bull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = AsyncMock(
            return_value=_result(
                "ok",
                [
                    _article(
                        title="Amazon earnings beat soaring growth",
                        published_at=datetime(2026, 5, 5, 13, 0, tzinfo=UTC),
                    )
                ],
            )
        )
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)

        out = await NewsStageAnalyzer().analyze(_ctx())

        fetch.assert_awaited_once()
        assert fetch.await_args.args[0] == "AMZN"
        assert fetch.await_args.args[1] == "us"
        assert out.signals.headline_count == 1
        assert out.verdict == StageVerdict.BULL

    @pytest.mark.asyncio
    async def test_empty_status_is_neutral_zero_headlines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            news_stage,
            "fetch_symbol_news",
            AsyncMock(return_value=_result("empty", [])),
        )
        out = await NewsStageAnalyzer().analyze(_ctx())
        assert out.verdict == StageVerdict.NEUTRAL
        assert out.signals.headline_count == 0

    @pytest.mark.asyncio
    async def test_error_status_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            news_stage,
            "fetch_symbol_news",
            AsyncMock(return_value=_result("error", [])),
        )
        out = await NewsStageAnalyzer().analyze(_ctx())
        assert out.verdict == StageVerdict.UNAVAILABLE
        assert out.confidence == 0

    @pytest.mark.asyncio
    async def test_kr_routes_to_kr_market(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = AsyncMock(return_value=_result("empty", []))
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        await NewsStageAnalyzer().analyze(
            _ctx(symbol="005930", instrument_type="equity_kr")
        )
        assert fetch.await_args.args[1] == "kr"
