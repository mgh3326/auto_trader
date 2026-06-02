# tests/services/test_research_news_service.py
"""Tests for research_news_service shim over symbol_news_service (ROB-423)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.services import research_news_service, symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _seam_article(provider: str = "naver") -> SymbolNewsArticle:
    return SymbolNewsArticle(
        provider=provider,
        market="kr",
        symbol="005930",
        external_article_id="001:123",
        title="삼성전자 호실적",
        source_name="한국경제",
        canonical_url="https://finance.naver.com/x",
        summary="요약" if provider == "finnhub" else None,
        published_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_maps_seam_to_normalized_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SymbolNewsFetchResult(
        symbol="005930",
        market="kr",
        provider="naver",
        status="ok",
        requested_limit=20,
        returned_count=1,
        articles=[_seam_article()],
    )
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(return_value=result),
    )

    out = await research_news_service.fetch_symbol_news("005930", "equity_kr", limit=20)

    assert len(out) == 1
    first = out[0]
    assert first.url == "https://finance.naver.com/x"
    assert first.title == "삼성전자 호실적"
    assert first.source == "한국경제"
    assert first.provider == "naver"
    assert first.summary is None
    assert isinstance(first.published_at, datetime)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_passes_market_for_us(monkeypatch: pytest.MonkeyPatch) -> None:
    seam = AsyncMock(
        return_value=SymbolNewsFetchResult(
            symbol="AAPL",
            market="us",
            provider="finnhub",
            status="empty",
            requested_limit=20,
            returned_count=0,
            articles=[],
        )
    )
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", seam)

    out = await research_news_service.fetch_symbol_news("AAPL", "equity_us", limit=20)

    assert out == []
    seam.assert_awaited_once()
    assert seam.await_args.args[1] == "us"  # market derived from instrument_type


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shim_returns_empty_for_unknown_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = AsyncMock()
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", seam)

    out = await research_news_service.fetch_symbol_news("X", "crypto", limit=20)

    assert out == []
    seam.assert_not_awaited()
