from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.unit
async def test_market_news_can_return_crypto_briefing_ranked_candidates():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    rows = [
        SimpleNamespace(
            id=1,
            title="OpenAI launches Linux coding model",
            url="https://decrypt.co/ai",
            source="Decrypt",
            feed_source="rss_decrypt",
            market="crypto",
            summary="Developer AI story without token, blockchain, or crypto market impact.",
            article_published_at=datetime(2026, 5, 1, 12, 0, 0),
            keywords=[],
            stock_symbol=None,
            stock_name=None,
        ),
        SimpleNamespace(
            id=2,
            title="Bitcoin ETF inflows rebound as BTC volatility rises",
            url="https://cointelegraph.com/btc-etf",
            source="Cointelegraph",
            feed_source="rss_cointelegraph",
            market="crypto",
            summary="Spot ETF flows and BTC volatility affect crypto market direction.",
            article_published_at=datetime(2026, 5, 1, 12, 5, 0),
            keywords=[],
            stock_symbol=None,
            stock_name=None,
        ),
    ]

    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=(rows, 2)),
    ):
        result = await _get_market_news_impl(
            market="crypto",
            hours=48,
            limit=2,
            briefing_filter=True,
        )

    assert result["count"] == 1
    assert result["total"] == 2
    assert result["briefing_filter"] is True
    assert result["briefing_summary"] == {
        "included": 1,
        "excluded": 1,
        "high": 1,
        "medium": 0,
        "low": 1,
    }
    assert result["news"][0]["title"].startswith("Bitcoin ETF")
    assert result["news"][0]["crypto_relevance"]["bucket"] == "high"
    assert result["excluded_news"][0]["title"].startswith("OpenAI")
    assert (
        result["excluded_news"][0]["crypto_relevance"]["noise_reason"]
        == "broad_tech_without_crypto_signal"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_market_news_default_keeps_raw_crypto_news_with_relevance_metadata():
    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    row = SimpleNamespace(
        id=1,
        title="OpenAI launches Linux coding model",
        url="https://decrypt.co/ai",
        source="Decrypt",
        feed_source="rss_decrypt",
        market="crypto",
        summary="Developer AI story without crypto market impact.",
        article_published_at=None,
        keywords=[],
        stock_symbol=None,
        stock_name=None,
    )

    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=([row], 1)),
    ):
        result = await _get_market_news_impl(market="crypto", limit=1)

    assert result["briefing_filter"] is False
    assert result["count"] == 1
    assert result["excluded_news"] == []
    assert result["news"][0]["crypto_relevance"]["bucket"] == "low"
