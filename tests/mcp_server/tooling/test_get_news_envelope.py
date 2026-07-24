# tests/mcp_server/tooling/test_get_news_envelope.py
"""get_news payload compatibility plus ROB-1048 freshness/provenance metadata."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.fundamentals import _news
from app.services import symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _naver_article() -> SymbolNewsArticle:
    raw = {
        "title": "삼성전자 호실적",
        "url": "https://finance.naver.com/item/news_read.naver?article_id=1&office_id=2",
        "source": "한국경제",
        "datetime": "2026-05-05",
    }
    return SymbolNewsArticle(
        provider="naver",
        market="kr",
        symbol="005930",
        external_article_id="2:1",
        title=raw["title"],
        source_name=raw["source"],
        canonical_url=raw["url"],
        summary=None,
        published_at=datetime(2026, 5, 5, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 1, tzinfo=UTC),
        provider_metadata={"source_item": raw},
    )


def _finnhub_article() -> SymbolNewsArticle:
    raw = {
        "title": "Apple beats earnings",
        "source": "Reuters",
        "datetime": "2026-05-05T12:00:00",
        "url": "https://x/aapl-1",
        "summary": "strong",
        "sentiment": "positive",
        "related": "AAPL",
    }
    return SymbolNewsArticle(
        provider="finnhub",
        market="us",
        symbol="AAPL",
        external_article_id="abc",
        title=raw["title"],
        source_name=raw["source"],
        canonical_url=raw["url"],
        summary=raw["summary"],
        published_at=datetime(2026, 5, 5, 12, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 5, 13, tzinfo=UTC),
        related_symbols=["AAPL"],
        provider_metadata={
            "sentiment": "positive",
            "related": "AAPL",
            "source_item": raw,
        },
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_envelope_adds_freshness_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = _naver_article()
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930", "kr", "naver", "ok", 10, 1, [art], excluded_count=0
            )
        ),
    )

    out = await _news.handle_get_news("005930", market="kr", limit=10)

    expected_payload = {
        "symbol": "005930",
        "market": "kr",
        "source": "naver",
        "count": 1,
        "excluded_count": 0,
        "news": [art.provider_metadata["source_item"]],
    }
    assert {key: out[key] for key in expected_payload} == expected_payload
    assert set(out) == {
        *expected_payload,
        "data_state",
        "derived_as_of",
        "fetched_at",
        "data_age_seconds",
        "cache_hit",
        "fallback_source",
        "provider_provenance",
    }
    assert out["data_state"] == "stale"
    assert out["derived_as_of"] == out["fetched_at"] == art.fetched_at.isoformat()
    assert out["data_age_seconds"] > _news.NEWS_FRESHNESS_MAX_AGE_SECONDS
    assert out["cache_hit"] is False
    assert out["fallback_source"] is None
    assert out["provider_provenance"] == [
        {
            "provider": "naver",
            "served_by": "naver",
            "mode": "live",
            "status": "ok",
            "error_code": None,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_exposes_relevance_block_and_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relevance = {
        "status": "confirmed",
        "relationship": "direct",
        "relevance": "high",
        "price_relevance": "catalyst",
        "score": 0.9,
        "reason": "본문이 NAVER 실적을 직접 다룸",
        "judged_by": "hermes",
        "judged_at": "2026-06-10T10:00:00+00:00",
        "hints": {"alias_match": ["네이버"]},
    }
    art = replace(
        _naver_article(),
        provider_metadata={
            **_naver_article().provider_metadata,
            "relevance": relevance,
        },
    )
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930", "kr", "naver", "ok", 10, 1, [art], excluded_count=4
            )
        ),
    )

    out = await _news.handle_get_news("005930", market="kr", limit=10)

    assert out["news"][0]["relevance"] == relevance
    assert out["excluded_count"] == 4
    assert "degraded" not in out
    assert "relevance" not in art.provider_metadata["source_item"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_kr_degraded_meta_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article = replace(_naver_article(), fetched_at=datetime.now(tz=UTC))
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930",
                "kr",
                "naver",
                "ok",
                10,
                1,
                [article],
                degraded=True,
                fetch_error="RuntimeError",
            )
        ),
    )
    out = await _news.handle_get_news("005930", market="kr", limit=10)
    assert out["degraded"] is True
    assert out["fetch_error"] == "RuntimeError"
    assert out["data_state"] == "degraded"
    assert out["cache_hit"] is True
    assert out["fallback_source"] == "news_articles"
    assert out["provider_provenance"] == [
        {
            "provider": "naver",
            "served_by": "news_articles",
            "mode": "fallback",
            "status": "error",
            "error_code": "RuntimeError",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_expired_fallback_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article = _naver_article()
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "005930",
                "kr",
                "naver",
                "ok",
                10,
                1,
                [article],
                degraded=True,
                fetch_error="TimeoutError",
            )
        ),
    )

    out = await _news.handle_get_news("005930", market="kr", limit=10)

    assert out["data_state"] == "stale"
    assert out["cache_hit"] is True
    assert out["fallback_source"] == "news_articles"
    assert out["provider_provenance"][0]["status"] == "error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_us_envelope_keys_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = _finnhub_article()
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "AAPL", "us", "finnhub", "ok", 10, 1, [art]
            )
        ),
    )

    out = await _news.handle_get_news("AAPL", market="us", limit=10)

    assert out["source"] == "finnhub"
    assert out["count"] == 1
    assert set(out["news"][0].keys()) == {
        "title",
        "source",
        "datetime",
        "url",
        "summary",
        "sentiment",
        "related",
    }
    assert out["news"][0]["sentiment"] == "positive"
    assert "relevance" not in out["news"][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_error_status_returns_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "AAPL", "us", "finnhub", "error", 10, 0, [], "RuntimeError"
            )
        ),
    )

    out = await _news.handle_get_news("AAPL", market="us", limit=10)

    assert out.get("error") or out.get("source") == "finnhub"
    assert "news" not in out or out.get("count", 0) == 0
    assert out["data_state"] == "missing"
    assert out["derived_as_of"] is None
    assert out["fetched_at"] is None
    assert out["data_age_seconds"] is None
    assert out["cache_hit"] is False
    assert out["fallback_source"] is None
    assert out["provider_provenance"][0]["mode"] == "none"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_authoritative_empty_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime.now(tz=UTC)
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(
            return_value=SymbolNewsFetchResult(
                "AAPL",
                "us",
                "finnhub",
                "empty",
                10,
                0,
                [],
                fetched_at=fetched_at,
            )
        ),
    )

    out = await _news.handle_get_news("AAPL", market="us", limit=10)

    assert out["count"] == 0
    assert out["news"] == []
    assert out["data_state"] == "fresh"
    assert out["derived_as_of"] == fetched_at.isoformat()
    assert out["provider_provenance"][0]["status"] == "empty"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_news_us_surfaces_relevance_and_degraded(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.mcp_server.tooling.fundamentals import _news
    from app.services.symbol_news_service import (
        SymbolNewsArticle,
        SymbolNewsFetchResult,
    )

    article = SymbolNewsArticle(
        provider="finnhub",
        market="us",
        symbol="AAPL",
        external_article_id="abc123",
        title="Cached headline",
        source_name="Reuters",
        canonical_url="https://r/cached",
        summary="cached summary",
        published_at=datetime(2026, 6, 10, 9, 0),
        fetched_at=datetime.now(tz=UTC),
        provider_metadata={
            "source_item": {"title": "Cached headline", "url": "https://r/cached"},
            "relevance": {"status": "pending"},
        },
    )
    result = SymbolNewsFetchResult(
        symbol="AAPL",
        market="us",
        provider="finnhub",
        status="ok",
        requested_limit=10,
        returned_count=1,
        articles=[article],
        excluded_count=3,
        degraded=True,
        fetch_error="TimeoutError",
    )
    monkeypatch.setattr(
        _news.symbol_news_service,
        "fetch_symbol_news",
        AsyncMock(return_value=result),
    )

    payload = await _news.handle_get_news("AAPL", market="us")

    assert payload["degraded"] is True
    assert payload["fetch_error"] == "TimeoutError"
    assert payload["excluded_count"] == 3
    assert payload["news"][0]["relevance"]["status"] == "pending"
