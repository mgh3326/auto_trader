# app/services/news_radar_classifier.py
"""Deterministic risk classifier for the Market Risk News Radar (ROB-109).

Pure functions. No I/O. No LLM. No DB. No broker calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["high", "medium", "low"]
RiskCategory = Literal[
    "geopolitical_oil",
    "macro_policy",
    "crypto_security",
    "earnings_bigtech",
    "korea_market",
]

# Severity is decided by which bucket fires first AND whether high-priority
# terms are present. Buckets are evaluated top-down; first match wins.
_GEOPOLITICAL_OIL_HIGH = (
    "uae",
    "iran",
    "israel",
    "hormuz",
    "gulf",
    "missile",
    "drone",
    "airstrike",
    "warship",
    "sanctions",
    "tanker",
    "ceasefire",
)
_GEOPOLITICAL_OIL_OIL = ("opec", "brent", "wti", "crude", "oil")
_MACRO_POLICY = (
    "fed",
    "fomc",
    "rate cut",
    "rate hike",
    "rates",
    "cpi",
    "pce",
    "tariff",
    "treasury yield",
    "treasury",
    "yields",
)
_CRYPTO_SECURITY = (
    "hack",
    "exploit",
    "sec",
    "etf",
    "binance",
    "coinbase",
    "stablecoin",
)
_EARNINGS_BIGTECH = (
    "earnings",
    "guidance",
    "nvidia",
    "microsoft",
    "apple",
    "alphabet",
    "google",
    "amazon",
    "meta",
    "tesla",
    "semiconductor",
    "chip",
)
_KOREA_MARKET = (
    "환율",
    "코스피",
    "코스닥",
    "반도체",
    "2차전지",
    "관세",
    "수출",
)

# Themes are independent of the section the item lands in; useful for chips.
_THEME_TERMS: dict[str, tuple[str, ...]] = {
    "oil": ("oil", "crude", "brent", "wti", "opec", "tanker"),
    "defense": ("missile", "drone", "airstrike", "warship", "sanctions"),
    "shipping": ("hormuz", "gulf", "tanker", "shipping"),
    "airlines": ("airline", "airlines"),
    "banks": ("bank", "banks", "lending", "credit"),
    "ai_semis": ("ai", "semiconductor", "chip", "nvidia"),
    "korea_macro": ("환율", "수출", "관세"),
    "korea_index": ("코스피", "코스닥"),
}


@dataclass(frozen=True)
class NewsRadarItemClassification:
    risk_category: RiskCategory | None
    severity: Severity
    matched_terms: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _haystack(article: Any) -> str:
    title = str(_field(article, "title") or "")
    summary = str(_field(article, "summary") or "")
    keywords = _field(article, "keywords") or []
    keyword_text = " ".join(str(k) for k in keywords)
    return f"{title} {summary} {keyword_text}".lower()


def _matches(haystack: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in haystack]


def _themes_for(haystack: str) -> list[str]:
    found: list[str] = []
    for theme, terms in _THEME_TERMS.items():
        if any(term in haystack for term in terms):
            found.append(theme)
    return found


def classify_news_radar_item(
    article: Any,
    *,
    briefing_score: int = 0,
) -> NewsRadarItemClassification:
    """Classify a news article into a risk category + severity.

    `briefing_score` is the existing market-news-briefing relevance score
    (0..100). It is only used to nudge severity up for already-highly-rated
    items inside non-geopolitical buckets.
    """
    haystack = _haystack(article)
    market = str(_field(article, "market") or "").lower()
    themes = _themes_for(haystack)

    geo_high = _matches(haystack, _GEOPOLITICAL_OIL_HIGH)
    geo_oil = _matches(haystack, _GEOPOLITICAL_OIL_OIL)
    if geo_high or geo_oil:
        severity: Severity = "high" if geo_high else "medium"
        return NewsRadarItemClassification(
            risk_category="geopolitical_oil",
            severity=severity,
            matched_terms=sorted(set(geo_high + geo_oil)),
            themes=themes,
        )

    macro = _matches(haystack, _MACRO_POLICY)
    if macro:
        return NewsRadarItemClassification(
            risk_category="macro_policy",
            severity="medium",
            matched_terms=sorted(set(macro)),
            themes=themes,
        )

    if market == "crypto" or any(t in haystack for t in _CRYPTO_SECURITY):
        crypto = _matches(haystack, _CRYPTO_SECURITY)
        if crypto:
            high_terms = {"hack", "exploit"}
            severity = "high" if any(t in high_terms for t in crypto) else "medium"
            return NewsRadarItemClassification(
                risk_category="crypto_security",
                severity=severity,
                matched_terms=sorted(set(crypto)),
                themes=themes,
            )

    if market == "kr" or any(t in haystack for t in _KOREA_MARKET):
        kr = _matches(haystack, _KOREA_MARKET)
        if kr:
            severity = "medium" if briefing_score >= 40 else "low"
            return NewsRadarItemClassification(
                risk_category="korea_market",
                severity=severity,
                matched_terms=sorted(set(kr)),
                themes=themes,
            )

    earnings = _matches(haystack, _EARNINGS_BIGTECH)
    if earnings:
        return NewsRadarItemClassification(
            risk_category="earnings_bigtech",
            severity="medium",
            matched_terms=sorted(set(earnings)),
            themes=themes,
        )

    return NewsRadarItemClassification(
        risk_category=None,
        severity="low",
        matched_terms=[],
        themes=themes,
    )
