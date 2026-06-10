# tests/mcp_server/tooling/test_get_news_envelope.py
"""get_news envelope byte-compat regression after seam rewire (ROB-423)."""

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
async def test_get_news_kr_envelope_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert out == {
        "symbol": "005930",
        "market": "kr",
        "source": "naver",
        "count": 1,
        "excluded_count": 0,
        "news": [art.provider_metadata["source_item"]],
    }


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
                [_naver_article()],
                degraded=True,
                fetch_error="RuntimeError",
            )
        ),
    )
    out = await _news.handle_get_news("005930", market="kr", limit=10)
    assert out["degraded"] is True
    assert out["fetch_error"] == "RuntimeError"


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
