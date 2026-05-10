"""ROB-169 — KR news investment-relevance scorer.

Read-layer only. Mirrors the ROB-155 crypto/US shape: pure function over the
article view, no DB writes, no ingestion-time gating. The goal is to keep
market-wide KR investment context (KOSPI/IPO/금리/환율/반도체/정책 등) visible
even without a stock_symbol while suppressing pure society/crime/연예/스포츠
articles that have neither a stock_symbol nor a market-wide investment frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.news_entity_alias_data import (
    KR_BIG_CAP_GROUP_SYMBOLS,
    KR_BROAD_MARKET_TERMS,
    KR_CRIME_TERMS,
    KR_INVEST_KEYWORDS,
    KR_NOISE_TERMS,
    KR_SOCIETY_TERMS,
)

_INCLUDE_THRESHOLD = 35


@dataclass(frozen=True)
class KrNewsRelevance:
    score: int
    bucket: str
    category: str | None
    include_in_briefing: bool
    matched_terms: list[str]
    noise_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "bucket": self.bucket,
            "category": self.category,
            "include_in_briefing": self.include_in_briefing,
            "matched_terms": self.matched_terms,
            "noise_reason": self.noise_reason,
        }


_INTERNAL_TO_USER_CATEGORY: dict[str, str] = {
    "kr_macro": "kr_macro",
    "kr_index": "kr_index",
    "kr_industry": "kr_industry",
    "kr_policy": "kr_policy",
    "kr_listing": "kr_listing",
    "kr_symbol": "kr_symbol",
}


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _full_text(article: Any) -> tuple[str, str]:
    title = str(_field(article, "title") or "")
    summary = str(_field(article, "summary") or "")
    keywords = _field(article, "keywords") or []
    keyword_text = " ".join(str(k) for k in keywords if k)
    full = f"{title} {summary} {keyword_text}".lower()
    return title.lower(), full


def _bucket(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= _INCLUDE_THRESHOLD:
        return "medium"
    return "low"


def _has_symbol_anchor(article: Any) -> bool:
    """The caller is expected to pass article rows with stock_symbol attribute or key."""
    return bool(_field(article, "stock_symbol"))


def score_kr_news_article(article: Any) -> KrNewsRelevance:
    """Score one KR-market article for investment relevance.

    Returns a KrNewsRelevance whose `include_in_briefing` is True only when the
    article is investment-relevant (either symbol-anchored, market-wide, or
    industry/policy framed) AND not dominated by society/crime/noise terms.
    """
    title_lower, full_text = _full_text(article)

    matched_terms: list[str] = []
    score = 0

    if _has_symbol_anchor(article):
        score += 30
        symbol = str(_field(article, "stock_symbol") or "")
        matched_terms.append(f"symbol:{symbol}")
        category = "kr_symbol"
    else:
        category = None

    broad_hits = [t for t in KR_BROAD_MARKET_TERMS if t.lower() in full_text]
    invest_hits = [t for t in KR_INVEST_KEYWORDS if t.lower() in full_text]
    crime_hits = [t for t in KR_CRIME_TERMS if t.lower() in full_text]
    society_hits = [t for t in KR_SOCIETY_TERMS if t.lower() in full_text]
    noise_hits = [t for t in KR_NOISE_TERMS if t.lower() in full_text]

    title_broad_hits = [t for t in KR_BROAD_MARKET_TERMS if t.lower() in title_lower]
    title_invest_hits = [t for t in KR_INVEST_KEYWORDS if t.lower() in title_lower]

    score += min(45, len(broad_hits) * 15)
    score += min(15, len(title_broad_hits) * 15)
    score += min(30, len(invest_hits) * 10)
    score += min(15, len(title_invest_hits) * 15)

    matched_terms.extend(broad_hits)
    matched_terms.extend(invest_hits)

    if broad_hits and not category:
        category = "kr_index" if any("코스" in t or "kospi" in t or "kosdaq" in t for t in broad_hits) else "kr_macro"
    if invest_hits and not category:
        category = "kr_industry"

    if crime_hits or society_hits or noise_hits:
        # Society/crime/sports/celebrity/weather override unless a strong
        # investment frame is also present.
        noise_strength = len(crime_hits) * 3 + len(society_hits) * 2 + len(noise_hits)
        invest_strength = (
            (30 if _has_symbol_anchor(article) else 0)
            + len(broad_hits) * 3
            + len(invest_hits) * 4
        )
        if noise_strength >= invest_strength:
            score = min(score, 10)
            matched_terms.extend(crime_hits + society_hits + noise_hits)
            primary_noise = "kr_crime" if crime_hits else "kr_society" if society_hits else "kr_noise"
            return KrNewsRelevance(
                score=score,
                bucket=_bucket(score),
                category=None,
                include_in_briefing=False,
                matched_terms=sorted(set(matched_terms)),
                noise_reason=primary_noise,
            )

    score = max(0, min(100, score))
    # Symbol-anchored articles are always investment-relevant by definition.
    include = score >= _INCLUDE_THRESHOLD or _has_symbol_anchor(article)

    noise_reason: str | None = None
    if not include:
        noise_reason = "low_kr_relevance"

    return KrNewsRelevance(
        score=score,
        bucket=_bucket(score),
        category=category if include else None,
        include_in_briefing=include,
        matched_terms=sorted(set(matched_terms)),
        noise_reason=noise_reason,
    )


def user_facing_kr_category(internal_category: str | None) -> str | None:
    """Map an internal scoring category to a user-facing category enum value."""
    if internal_category is None:
        return None
    return _INTERNAL_TO_USER_CATEGORY.get(internal_category, internal_category)


def _kr_big_cap_overlap(symbols: list[str]) -> set[str]:
    """Return the subset of provided symbols that are KR big-cap reference symbols.

    Currently unused by the scorer; reserved for future scope-based demotion if
    we extend KR scope classification analogous to ROB-155 US scope.
    """
    return {s for s in symbols if s in KR_BIG_CAP_GROUP_SYMBOLS}
