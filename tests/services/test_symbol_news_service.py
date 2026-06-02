# tests/services/test_symbol_news_service.py
"""Tests for app.services.symbol_news_service (ROB-423 PR1)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services import symbol_news_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_returns_normalized_articles_with_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [
        {
            "title": "삼성전자 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=123&office_id=001",
            "source": "한국경제",
            "datetime": "2026-05-05T09:30",
        },
        {"title": "", "url": "", "source": "", "datetime": None},  # dropped (no url/title)
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(return_value=raw),
    )

    result = await symbol_news_service.fetch_symbol_news("005930", "kr", limit=20)

    assert result.status == "ok"
    assert result.provider == "naver"
    assert result.returned_count == 1
    art = result.articles[0]
    assert art.symbol == "005930"
    assert art.market == "kr"
    assert art.title == "삼성전자 호실적"
    assert art.source_name == "한국경제"
    assert art.canonical_url.endswith("article_id=123&office_id=001")
    assert art.external_article_id == "001:123"
    assert isinstance(art.published_at, datetime)
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_finnhub_preserves_source_item_and_sentiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "symbol": "AAPL",
        "market": "us",
        "source": "finnhub",
        "count": 1,
        "news": [
            {
                "title": "Apple beats earnings",
                "source": "Reuters",
                "datetime": "2026-05-05T12:00:00",
                "url": "https://x/aapl-1",
                "summary": "strong quarter",
                "sentiment": "positive",
                "related": "AAPL,MSFT",
            }
        ],
    }
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=payload),
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.provider == "finnhub"
    art = result.articles[0]
    assert art.external_article_id is not None  # url hash
    assert art.related_symbols == ["AAPL", "MSFT"]
    assert art.provider_metadata["sentiment"] == "positive"
    assert art.provider_metadata["source_item"] == payload["news"][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_provider_result_is_status_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "empty"
    assert result.returned_count == 0
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_error_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "error"
    assert result.error_code == "RuntimeError"
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_market_is_unavailable() -> None:
    result = await symbol_news_service.fetch_symbol_news("FOO", "jp")
    assert result.status == "unavailable"
    assert result.error_code == "unsupported_market"
