from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.news_handlers import _get_market_news_impl
from app.services.news_text import NEWS_RESPONSE_MAX_CHARS, NEWS_SUMMARY_MAX_CHARS

_PATCH_TARGET = "app.mcp_server.tooling.news_handlers.get_news_articles"


def _row(
    article_id: int,
    title: str,
    *,
    feed_source: str = "rss_test",
    market: str = "us",
    summary: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source="Test Source",
        feed_source=feed_source,
        market=market,
        summary=summary,
        article_published_at=datetime(2026, 6, 11, 9, 0, 0),
        keywords=[],
        stock_symbol=None,
        stock_name=None,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_summary_truncates_to_max_chars():
    long_summary = "A" * 500  # no whitespace -> deterministic length
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary=long_summary)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10)  # detail default

    summary = result["news"][0]["summary"]
    assert summary is not None
    assert len(summary) <= NEWS_SUMMARY_MAX_CHARS == 240
    assert summary.endswith("…")
    assert summary != long_summary


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_headline_only_drops_summary():
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary="B" * 500)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(
            market="us", limit=10, detail="headline_only"
        )

    item = result["news"][0]
    assert "summary" not in item
    assert item["title"] == "Fed holds rates steady as inflation cools"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_full_keeps_untruncated_summary():
    long_summary = "C" * 500
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary=long_summary)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10, detail="full")

    summary = result["news"][0]["summary"]
    assert summary == long_summary
    assert len(summary) == 500
    assert not summary.endswith("…")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_briefing_sections_carry_only_ids_and_relevance():
    rows = [
        _row(1, "Fed rate cut hopes lift S&P 500 futures before CPI report"),
        _row(2, "Nvidia AI chip demand lifts Nasdaq semiconductor stocks"),
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 2))):
        result = await _get_market_news_impl(
            market="us", limit=10, briefing_filter=True
        )

    sections = result["briefing_sections"]
    assert sections
    news_ids = {n["id"] for n in result["news"]}
    for section in sections:
        # no full article dict re-embedded
        assert "items" not in section
        assert "summary" not in section
        assert "url" not in section
        assert set(section) == {"section_id", "title", "count", "article_ids", "relevance"}
        assert isinstance(section["article_ids"], list)
        assert all(isinstance(aid, int) for aid in section["article_ids"])
        assert isinstance(section["relevance"], list)
        assert len(section["relevance"]) == len(section["article_ids"]) == section["count"]
        # relevance dicts are scoring metadata, not article bodies
        for rel in section["relevance"]:
            assert "score" in rel
            assert "title" not in rel  # no article title leaking through
        # ids point at bodies that live in news[]
        for aid in section["article_ids"]:
            assert aid in news_ids
    # bodies are present exactly once, in news[]
    assert all("title" in n for n in result["news"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_excluded_news_capped_to_limit_with_total():
    rows = [
        _row(1, "Sponsored: The 5 best coins to buy now"),
        _row(2, "XRP Price Prediction: Could XRP reach $10 by 2027?"),
        _row(3, "My plumber charged $160 to fix a problem — do I pay again?"),
        _row(4, "Sponsored: The 5 best coins to buy now"),
        _row(5, "XRP Price Prediction: Could XRP reach $10 by 2027?"),
        _row(6, "Fed holds rates steady as inflation cools"),
        _row(7, "Nvidia AI chip demand lifts Nasdaq semiconductor stocks"),
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 7))):
        result = await _get_market_news_impl(market="us", limit=2)

    assert result["excluded_total"] == 5
    assert len(result["excluded_news"]) == 2  # capped to limit
    assert result["count"] == 2  # two clean items survive
    assert all("excluded_reason" in e for e in result["excluded_news"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_oversized_payload_truncated_for_size_stays_under_cap():
    # detail="full" keeps big summaries -> blows past the size cap.
    big = "X" * 3000
    rows = [
        _row(i, f"Fed holds rates steady as inflation cools {i}", summary=big)
        for i in range(1, 8)
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 7))):
        result = await _get_market_news_impl(market="us", limit=20, detail="full")

    assert result["truncated_for_size"] is True
    assert result["degraded_reason"]
    assert result["status"] == "truncated_for_size"
    assert "size_truncation" in result
    assert result["size_truncation"]["dropped_news"] > 0
    # honest accounting: count matches what actually remains
    assert result["count"] == len(result["news"]) < 7
    # the serialized response actually fits under the hard cap
    assert len(json.dumps(result, default=str)) <= NEWS_RESPONSE_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.unit
async def test_small_payload_not_flagged_truncated():
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary="short")]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10)

    assert result["truncated_for_size"] is False
    assert "size_truncation" not in result
