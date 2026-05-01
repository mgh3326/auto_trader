from __future__ import annotations

from types import SimpleNamespace

import pytest


def _article(
    title: str,
    *,
    summary: str | None = None,
    feed_source: str = "rss_decrypt",
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        summary=summary,
        feed_source=feed_source,
        keywords=keywords or [],
    )


@pytest.mark.unit
def test_score_crypto_news_prioritizes_bitcoin_etf_market_impact():
    from app.services.crypto_news_relevance_service import score_crypto_news_article

    scored = score_crypto_news_article(
        _article(
            "Spot Bitcoin ETF outflows top $490M as BTC tests key support",
            summary="ETF flow and Bitcoin price support levels drive crypto market risk.",
            feed_source="rss_coindesk",
        )
    )

    assert scored.score >= 80
    assert scored.bucket == "high"
    assert scored.category in {"etf_institutional", "market_price"}
    assert scored.include_in_briefing is True
    assert scored.noise_reason is None


@pytest.mark.unit
def test_score_crypto_news_keeps_crypto_price_warning_from_decrypt():
    from app.services.crypto_news_relevance_service import score_crypto_news_article

    scored = score_crypto_news_article(
        _article(
            "Bitcoin Crash Incoming? April Surge Was Built on Shaky Ground, Analysts Warn",
            feed_source="rss_decrypt",
        )
    )

    assert scored.include_in_briefing is True
    assert scored.bucket in {"medium", "high"}
    assert scored.category == "market_price"


@pytest.mark.unit
def test_score_crypto_news_demotes_broader_ai_tech_from_decrypt():
    from app.services.crypto_news_relevance_service import score_crypto_news_article

    scored = score_crypto_news_article(
        _article(
            "OpenAI launches new coding model for Linux developers",
            summary="The release focuses on developer tools and AI assistants without crypto links.",
            feed_source="rss_decrypt",
        )
    )

    assert scored.score < 40
    assert scored.bucket == "low"
    assert scored.include_in_briefing is False
    assert scored.noise_reason == "broad_tech_without_crypto_signal"


@pytest.mark.unit
def test_rank_crypto_news_for_briefing_orders_relevant_items_and_keeps_exclusions():
    from app.services.crypto_news_relevance_service import rank_crypto_news_for_briefing

    btc = _article(
        "Bitcoin ETF inflows rebound as BTC volatility rises",
        feed_source="rss_cointelegraph",
    )
    ai = _article("OpenAI launches Linux coding model", feed_source="rss_decrypt")
    stablecoin = _article(
        "Stablecoins overtake Bitcoin in Latin America purchases",
        feed_source="rss_decrypt",
    )

    result = rank_crypto_news_for_briefing([ai, stablecoin, btc], limit=2)

    assert [item.article.title for item in result.included] == [
        btc.title,
        stablecoin.title,
    ]
    assert [item.article.title for item in result.excluded] == [ai.title]
    assert result.summary == {
        "included": 2,
        "excluded": 1,
        "high": 2,
        "medium": 0,
        "low": 1,
    }
