# tests/test_news_radar_classifier.py
from dataclasses import dataclass

import pytest

from app.services.news_radar_classifier import (
    NewsRadarItemClassification,
    classify_news_radar_item,
)


@dataclass
class FakeArticle:
    title: str = ""
    summary: str | None = None
    keywords: list[str] | None = None
    feed_source: str | None = None
    stock_symbol: str | None = None
    market: str = "us"


@pytest.mark.unit
def test_geopolitical_oil_high_severity_from_uae_strike() -> None:
    article = FakeArticle(
        title="UAE airstrike on tanker in Hormuz pushes Brent higher",
        summary="OPEC monitors crude shipping risk after drone attack",
        market="us",
    )

    result = classify_news_radar_item(article, briefing_score=12)

    assert result.risk_category == "geopolitical_oil"
    assert result.severity == "high"
    assert "UAE" in result.matched_terms or "uae" in result.matched_terms
    assert "oil" in result.themes


@pytest.mark.unit
def test_macro_policy_medium_severity_from_fomc_cpi() -> None:
    article = FakeArticle(
        title="Fed signals rate cut as CPI cools; Treasury yields slip",
        market="us",
    )
    result = classify_news_radar_item(article, briefing_score=40)
    assert result.risk_category == "macro_policy"
    assert result.severity == "medium"


@pytest.mark.unit
def test_crypto_security_from_exchange_hack() -> None:
    article = FakeArticle(
        title="Binance reports $40M exploit on stablecoin bridge",
        market="crypto",
    )
    result = classify_news_radar_item(article, briefing_score=0)
    assert result.risk_category == "crypto_security"
    assert result.severity in {"high", "medium"}


@pytest.mark.unit
def test_korea_market_section_from_korean_terms() -> None:
    article = FakeArticle(
        title="환율 급등에 코스피 약세, 반도체 수출 둔화 우려",
        market="kr",
    )
    result = classify_news_radar_item(article, briefing_score=40)
    assert result.risk_category == "korea_market"
    assert result.severity == "medium"


@pytest.mark.unit
def test_returns_low_severity_when_no_keywords_match() -> None:
    article = FakeArticle(title="Local museum reopens after renovation", market="us")
    result = classify_news_radar_item(article, briefing_score=0)
    assert result.risk_category is None
    assert result.severity == "low"
    assert result.matched_terms == []


@pytest.mark.unit
def test_classification_is_a_dataclass_with_themes_list() -> None:
    article = FakeArticle(
        title="Iran sanctions tighten as missile drills cross Gulf shipping lanes",
        market="us",
    )
    result = classify_news_radar_item(article, briefing_score=0)
    assert isinstance(result, NewsRadarItemClassification)
    assert isinstance(result.themes, list)
    assert (
        "shipping" in result.themes
        or "oil" in result.themes
        or "defense" in result.themes
    )
