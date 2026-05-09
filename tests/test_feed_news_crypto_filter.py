from __future__ import annotations

from app.schemas.invest_feed_news import FeedNewsItem, NewsRelatedSymbol
from app.services.crypto_news_relevance_service import (
    score_crypto_news_article,
    user_facing_category,
)
from app.services.invest_view_model.feed_news_service import (
    _relation_from_related_symbols,
)


def test_crypto_user_facing_category_maps_etf_and_regulation_to_etf_regulatory():
    relevance = score_crypto_news_article(
        {
            "title": "SEC delays spot Bitcoin ETF decision",
            "summary": "BlackRock and Grayscale await regulatory approval for crypto ETF products.",
            "feed_source": "rss_coindesk",
            "keywords": ["bitcoin", "ETF", "SEC"],
        }
    )

    assert relevance.include_in_briefing is True
    assert user_facing_category(relevance.category) == "etf_regulatory"


def test_crypto_listing_delisting_category_is_supported():
    relevance = score_crypto_news_article(
        {
            "title": "Coinbase listing sends SOL trading volume higher",
            "summary": "A new listing and market support increased liquidity for Solana.",
            "feed_source": "rss_cointelegraph",
        }
    )

    assert relevance.include_in_briefing is True
    assert user_facing_category(relevance.category) == "listing_delisting"


def test_crypto_broad_ai_semiconductor_story_is_noise():
    relevance = score_crypto_news_article(
        {
            "title": "Nvidia earnings lift semiconductor stocks after GPU demand surges",
            "summary": "Chip maker shares rallied on artificial intelligence demand without blockchain or token support.",
            "feed_source": "rss_decrypt",
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason == "broad_tech_without_crypto_signal"
    assert relevance.score < 40


def test_feed_item_can_express_crypto_category_and_noise_reason():
    item = FeedNewsItem(
        id=2,
        title="OpenAI releases developer tool without crypto support",
        market="crypto",
        url="https://example.com/crypto-noise",
        category="low_relevance",
        noiseReason="broad_tech_without_crypto_signal",
        tags=["crypto_low_relevance"],
        relatedSymbols=[],
    )

    assert item.category == "low_relevance"
    assert item.noiseReason == "broad_tech_without_crypto_signal"
    assert _relation_from_related_symbols(item.relatedSymbols) == "none"


def test_relation_from_related_symbols_still_handles_related_crypto_symbols():
    related = [
        NewsRelatedSymbol(
            symbol="BTC",
            market="crypto",
            displayName="Bitcoin",
            relation="watchlist",
        )
    ]

    assert _relation_from_related_symbols(related) == "watchlist"
