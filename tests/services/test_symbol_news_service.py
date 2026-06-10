# tests/services/test_symbol_news_service.py
"""Tests for app.services.symbol_news_service (ROB-423 PR1)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import symbol_news_service


def _stored(article_id: int, url: str, title: str, status: str = "pending"):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="매일경제",
        published_at=datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": status,
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
    )


def _patch_store(monkeypatch, *, stored, excluded_count=0):
    upsert = AsyncMock()
    load = AsyncMock(return_value=(stored, excluded_count))
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_kr_feed_articles", upsert
    )
    monkeypatch.setattr(symbol_news_service.symbol_news_store, "load_symbol_news", load)
    # AsyncSessionLocal() 컨텍스트를 가짜 세션으로 대체
    fake_session = MagicMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )
    return upsert, load


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
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    _patch_store(
        monkeypatch,
        stored=[_stored(123, raw[0]["url"], raw[0]["title"])],
    )

    result = await symbol_news_service.fetch_symbol_news("005930", "kr", limit=20)

    assert result.status == "ok"
    assert result.provider == "naver"
    art = result.articles[0]
    assert art.symbol == "005930"
    assert art.market == "kr"
    assert art.title == "삼성전자 호실적"
    assert art.external_article_id == "001:123"
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_persists_then_serves_db_state(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 D2SF 투자",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=1&office_id=009",
            "source": "매일경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    upsert, _ = _patch_store(
        monkeypatch,
        stored=[_stored(1, raw[0]["url"], raw[0]["title"])],
        excluded_count=3,
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.excluded_count == 3
    assert result.degraded is False
    upsert.assert_awaited_once()
    art = result.articles[0]
    assert art.provider_metadata["relevance"]["status"] == "pending"
    # 현재 fetch 윈도우에 있던 기사는 원본 source_item 보존
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_row_outside_window_gets_reconstructed_source_item(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    _patch_store(
        monkeypatch, stored=[_stored(7, "https://x/old-article", "지난주 네이버 기사")]
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    item = result.articles[0].provider_metadata["source_item"]
    assert item["title"] == "지난주 네이버 기사"
    assert item["url"] == "https://x/old-article"
    assert "datetime" in item and "source" in item


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_fetch_failure_serves_db_cache_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    _patch_store(monkeypatch, stored=[_stored(1, "https://x/cached", "캐시 기사")])

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.degraded is True
    assert result.fetch_error == "RuntimeError"
    assert result.articles[0].title == "캐시 기사"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_failure_degrades_to_on_demand_pending(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=9&office_id=001",
            "source": "한국경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"  # 도구는 DB 때문에 죽지 않는다
    assert result.articles[0].provider_metadata["relevance"]["status"] == "pending"
    assert result.excluded_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_both_fetch_and_db_down_is_error(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "error"


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
    # KR case with empty fetch and empty DB
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    _patch_store(monkeypatch, stored=[])
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "empty"
    assert result.returned_count == 0
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_error_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If fetch fails but DB is empty, it's an error status
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    _patch_store(monkeypatch, stored=[])
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
