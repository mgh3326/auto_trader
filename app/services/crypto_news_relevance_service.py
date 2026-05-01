from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CryptoNewsRelevance:
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


@dataclass(frozen=True)
class RankedCryptoNewsItem:
    article: Any
    relevance: CryptoNewsRelevance


@dataclass(frozen=True)
class CryptoBriefingRanking:
    included: list[RankedCryptoNewsItem]
    excluded: list[RankedCryptoNewsItem]
    summary: dict[str, int]


_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "etf_institutional": (
        "etf",
        "spot bitcoin etf",
        "inflow",
        "outflow",
        "blackrock",
        "grayscale",
        "institutional",
        "treasury",
        "reserve",
    ),
    "market_price": (
        "crypto",
        "bitcoin",
        "btc",
        "ether",
        "ethereum",
        "eth",
        "price",
        "volatility",
        "support",
        "resistance",
        "liquidation",
        "open interest",
        "market",
        "crash",
        "surge",
        "rally",
        "momentum",
        "bull",
        "bear",
    ),
    "regulation_policy": (
        "sec",
        "cftc",
        "senate",
        "congress",
        "regulation",
        "regulatory",
        "lawsuit",
        "ban",
        "policy",
        "tether",
    ),
    "security_risk": (
        "hack",
        "hacker",
        "exploit",
        "drain",
        "stolen",
        "breach",
        "scam",
        "wallet",
        "protocol",
    ),
    "stablecoin_defi": (
        "stablecoin",
        "defi",
        "exchange",
        "dex",
        "token",
        "staking",
        "yield",
        "chain",
        "blockchain",
        "web3",
        "onchain",
        "on-chain",
    ),
}

_BROAD_TECH_TERMS = (
    "openai",
    "ai model",
    "artificial intelligence",
    "linux",
    "developer tool",
    "coding model",
    "chatgpt",
    "software",
)

_NEGATED_CRYPTO_PHRASES = (
    "without crypto",
    "no crypto",
    "without token",
    "without blockchain",
    "no blockchain",
)

_STRONG_FEEDS = {"rss_cointelegraph", "rss_coindesk", "rss_bitcoin_magazine"}
_BROADER_FEEDS = {"rss_decrypt"}


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _text(article: Any) -> tuple[str, str]:
    title = str(_field(article, "title") or "")
    summary = str(_field(article, "summary") or "")
    keywords = _field(article, "keywords") or []
    keyword_text = " ".join(str(k) for k in keywords)
    full = f"{title} {summary} {keyword_text}".lower()
    for phrase in _NEGATED_CRYPTO_PHRASES:
        full = full.replace(phrase, " ")
    return title.lower(), full


def _bucket(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def score_crypto_news_article(article: Any) -> CryptoNewsRelevance:
    """Score one crypto-market article for user-facing briefing relevance.

    This is intentionally read-layer only: it does not decide whether raw news should
    be stored. The goal is to keep broad tech items available while hiding them from
    concise crypto briefings unless they contain clear crypto-market signals.
    """

    title, full_text = _text(article)
    feed_source = str(_field(article, "feed_source") or "").lower()

    category_hits: dict[str, list[str]] = {}
    matched_terms: list[str] = []
    for category, terms in _CATEGORY_TERMS.items():
        hits = [term for term in terms if term in full_text]
        if hits:
            category_hits[category] = hits
            matched_terms.extend(hits)

    score = 0
    if feed_source in _STRONG_FEEDS:
        score += 20
    elif feed_source in _BROADER_FEEDS:
        score += 5

    for hits in category_hits.values():
        score += min(40, len(hits) * 15)
        # Title hits are more likely to be the article thesis.
        score += min(25, sum(15 for term in hits if term in title))

    broad_hits = [term for term in _BROAD_TECH_TERMS if term in full_text]
    title_crypto_hits = [
        term for terms in _CATEGORY_TERMS.values() for term in terms if term in title
    ]
    if broad_hits and not title_crypto_hits:
        # Decrypt and similar feeds can publish broader AI/software stories that
        # mention crypto only in boilerplate or negated context. Do not let summary
        # noise promote them into a market briefing.
        category_hits = {}
        matched_terms = []
        score = min(score, 15)
    elif broad_hits and not category_hits:
        score -= 25

    score = max(0, min(100, score))
    primary_category = None
    if category_hits:
        primary_category = max(category_hits, key=lambda cat: len(category_hits[cat]))

    noise_reason = None
    include = score >= 40
    if not include and broad_hits:
        noise_reason = "broad_tech_without_crypto_signal"
    elif not include:
        noise_reason = "low_crypto_relevance"

    return CryptoNewsRelevance(
        score=score,
        bucket=_bucket(score),
        category=primary_category,
        include_in_briefing=include,
        matched_terms=sorted(set(matched_terms)),
        noise_reason=noise_reason,
    )


def rank_crypto_news_for_briefing(
    articles: list[Any],
    *,
    limit: int | None = None,
) -> CryptoBriefingRanking:
    scored = [
        RankedCryptoNewsItem(
            article=article, relevance=score_crypto_news_article(article)
        )
        for article in articles
    ]
    included = [item for item in scored if item.relevance.include_in_briefing]
    excluded = [item for item in scored if not item.relevance.include_in_briefing]

    included.sort(key=lambda item: item.relevance.score, reverse=True)
    excluded.sort(key=lambda item: item.relevance.score, reverse=True)

    if limit is not None and limit >= 0:
        included = included[:limit]

    summary = {
        "included": len(included),
        "excluded": len(excluded),
        "high": sum(1 for item in scored if item.relevance.bucket == "high"),
        "medium": sum(1 for item in scored if item.relevance.bucket == "medium"),
        "low": sum(1 for item in scored if item.relevance.bucket == "low"),
    }
    return CryptoBriefingRanking(included=included, excluded=excluded, summary=summary)
