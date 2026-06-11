"""ROB-502 step 5: get_market_news MCP surface is quality-gated by default.

The non-briefing path must no longer return the raw DB list: noise-tagged
items move to excluded_news with reasons, and degraded states are explicit
(`no_meaningful_items` / `no_recent_articles`) instead of silent empties.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _row(
    article_id: int, title: str, *, feed_source: str = "rss_test"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source="Test Source",
        feed_source=feed_source,
        market="us",
        summary=None,
        article_published_at=datetime(2026, 6, 11, 9, 0, 0),
        keywords=[],
        stock_symbol=None,
        stock_name=None,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_path_excludes_noise_with_reason():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    rows = [
        _row(1, "Fed holds rates steady as inflation cools"),
        _row(
            2,
            "My plumber charged $160 to fix a problem — do I pay again?",
            feed_source="rss_marketwatch_topstories",
        ),
    ]
    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=(rows, 2)),
    ):
        result = await _get_market_news_impl(market="us", limit=10)

    assert result["status"] == "ok"
    assert [n["id"] for n in result["news"]] == [1]
    [excluded] = result["excluded_news"]
    assert excluded["id"] == 2
    assert excluded["excluded_reason"].startswith("noise:")
    assert "personal_finance" in excluded["excluded_reason"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_all_noise_yields_no_meaningful_items():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    rows = [
        _row(1, "Sponsored: The 5 best coins to buy now"),
        _row(2, "XRP Price Prediction: Could XRP reach $10 by 2027?"),
    ]
    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=(rows, 2)),
    ):
        result = await _get_market_news_impl(market="crypto", limit=10)

    assert result["status"] == "no_meaningful_items"
    assert result["news"] == []
    assert len(result["excluded_news"]) == 2
    assert result["degraded_reason"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_window_yields_no_recent_articles():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=([], 0)),
    ):
        result = await _get_market_news_impl(market="us", hours=24, limit=10)

    assert result["status"] == "no_recent_articles"
    assert result["news"] == []
    assert result["degraded_reason"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_briefing_path_reports_status_and_keeps_sections():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    rows = [
        _row(1, "Fed holds rates steady as inflation cools"),
        _row(2, "Nvidia AI chip demand lifts Nasdaq semiconductor stocks"),
    ]
    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=(rows, 2)),
    ):
        result = await _get_market_news_impl(
            market="us", limit=10, briefing_filter=True
        )

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["briefing_sections"]
