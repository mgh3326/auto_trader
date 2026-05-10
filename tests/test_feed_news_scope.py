from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.invest_feed_news import FeedNewsItem
from app.services.news_entity_matcher import (
    classify_article_scope,
    match_symbols_for_article,
)


def _classify(title: str, summary: str = ""):
    matches = match_symbols_for_article(title=title, summary=summary, market="us")
    return classify_article_scope(title, summary=summary, market="us", matches=matches)


def test_us_broad_market_big_tech_article_is_market_wide_and_demotes_symbols():
    result = _classify(
        "S&P 500 rallies as big tech climbs with Apple, Microsoft and Google",
        "The Nasdaq rose broadly as Apple, Microsoft, Google and Tesla gained on rate cut hopes.",
    )

    assert result.scope == "market_wide"
    assert "broad_market" in result.tags
    assert "big_tech_group" in result.tags
    assert {"AAPL", "MSFT", "GOOGL", "TSLA"}.issubset(set(result.demoted_symbols))


def test_us_single_company_anchor_stays_symbol_specific():
    result = _classify(
        "Apple reports record Q4 earnings and raises iPhone guidance",
        "Apple revenue beat consensus and management lifted guidance.",
    )

    assert result.scope == "symbol_specific"
    assert result.demoted_symbols == []


def test_us_broad_frame_with_specific_non_big_tech_anchor_is_mixed():
    matches = match_symbols_for_article(
        title="Stock market rally pauses as AMD reports earnings above guidance",
        summary="The Nasdaq was mixed, but AMD revenue and guidance topped estimates.",
        market="us",
    )
    result = classify_article_scope(
        "Stock market rally pauses as AMD reports earnings above guidance",
        summary="The Nasdaq was mixed, but AMD revenue and guidance topped estimates.",
        market="us",
        matches=matches,
    )

    assert result.scope == "mixed"
    assert "broad_market" in result.tags
    assert result.demoted_symbols == []


def test_classify_article_scope_is_noop_for_non_us_markets():
    result = classify_article_scope(
        "KOSPI rallies as Samsung and SK Hynix climb",
        summary="Broad market gains followed chip-sector optimism.",
        market="kr",
        matches=[],
    )

    assert result.scope == "symbol_specific"
    assert result.tags == []
    assert result.demoted_symbols == []


def test_feed_news_item_additive_fields_have_defaults_and_forbid_unknowns():
    item = FeedNewsItem(
        id=1,
        title="Example",
        market="us",
        url="https://example.com/news",
    )

    assert item.scope == "symbol_specific"
    assert item.tags == []
    assert item.category is None
    assert item.noiseReason is None

    with pytest.raises(ValidationError):
        FeedNewsItem(
            id=1,
            title="Example",
            market="us",
            url="https://example.com/news",
            unexpected=True,
        )


def test_feed_news_item_accepts_kr_market_wide_scope():
    item = FeedNewsItem(
        id=42,
        title="코스피 회복",
        market="kr",
        url="https://example.com/kr/42",
        scope="kr_market_wide",
    )
    assert item.scope == "kr_market_wide"


def test_feed_news_item_source_market_field_present_and_matches_market():
    """ROB-172: FeedNewsItem must expose `sourceMarket` (the article's feed
    market) alongside the legacy `market` field. The two values are equal
    during the backward-compat window; once the frontend migrates, the legacy
    `market` field can be retired in a separate ticket.
    """
    item = FeedNewsItem(
        id=9659,
        title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
        market="kr",
        sourceMarket="kr",
        url="https://example.com/news/9659",
    )

    assert item.market == "kr"
    assert item.sourceMarket == "kr"
    # `extra="forbid"` must continue to reject unknown fields.
    with pytest.raises(ValidationError):
        FeedNewsItem(
            id=9659,
            title="x",
            market="kr",
            sourceMarket="kr",
            url="https://example.com/news/9659",
            unknownField=True,
        )
