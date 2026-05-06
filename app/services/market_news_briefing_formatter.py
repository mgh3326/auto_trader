from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.crypto_news_relevance_service import score_crypto_news_article


@dataclass(frozen=True)
class BriefingRelevance:
    score: int
    section_id: str | None
    section_title: str | None
    include_in_briefing: bool
    matched_terms: list[str]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "section_id": self.section_id,
            "section_title": self.section_title,
            "include_in_briefing": self.include_in_briefing,
            "matched_terms": self.matched_terms,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BriefingItem:
    article: Any
    relevance: BriefingRelevance

    def as_dict(self) -> dict[str, Any]:
        return {"relevance": self.relevance.as_dict()}


@dataclass(frozen=True)
class BriefingSection:
    section_id: str
    title: str
    items: list[BriefingItem]

    def as_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "count": len(self.items),
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class MarketNewsBriefing:
    market: str
    sections: list[BriefingSection]
    excluded: list[BriefingItem]
    summary: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "sections": [section.as_dict() for section in self.sections],
            "excluded": [item.as_dict() for item in self.excluded],
            "summary": self.summary,
        }


@dataclass(frozen=True)
class _SectionRule:
    section_id: str
    title: str
    terms: tuple[str, ...]


_US_RULES: tuple[_SectionRule, ...] = (
    _SectionRule(
        "macro_fed",
        "Macro/Fed",
        (
            "fed",
            "fomc",
            "rate",
            "rates",
            "cpi",
            "inflation",
            "treasury",
            "yield",
            "yields",
            "s&p",
            "futures",
            "jobs report",
            "payrolls",
        ),
    ),
    _SectionRule(
        "finance_credit_rates",
        "Finance / Credit / Rates",
        (
            "finance",
            "financial",
            "credit",
            "credit market",
            "credit markets",
            "bank",
            "banks",
            "banking",
            "lending",
            "loan",
            "loans",
            "debt",
            "bond",
            "bonds",
            "liquidity",
            "default",
            "spreads",
            "regional bank",
            "commercial real estate",
        ),
    ),
    _SectionRule(
        "big_tech",
        "Big Tech / AI / Semis",
        (
            "microsoft",
            "apple",
            "alphabet",
            "google",
            "amazon",
            "meta",
            "tesla",
            "nvidia",
            "nasdaq",
            "ai",
            "chip",
            "chips",
            "semiconductor",
            "semis",
        ),
    ),
    _SectionRule(
        "earnings",
        "Earnings / Guidance",
        (
            "earnings",
            "guidance",
            "estimate",
            "estimates",
            "quarterly",
            "revenue",
            "eps",
            "profit",
        ),
    ),
    _SectionRule(
        "market_sentiment",
        "Market Sentiment",
        (
            "rally",
            "selloff",
            "sell-off",
            "volatility",
            "risk-on",
            "risk off",
            "risk-off",
            "dow",
            "s&p 500",
            "market sentiment",
        ),
    ),
    _SectionRule(
        "watchlist_analyst",
        "Watchlist / Analyst",
        (
            "analyst",
            "upgrade",
            "downgrade",
            "price target",
            "target price",
            "rating",
            "watchlist",
        ),
    ),
)

_KR_RULES: tuple[_SectionRule, ...] = (
    _SectionRule(
        "preopen_headlines",
        "장전 주요 뉴스",
        ("장전", "코스피", "코스닥", "환율", "금리", "지수", "시황", "미 증시"),
    ),
    _SectionRule(
        "sector_theme",
        "업종/테마",
        (
            "업종",
            "테마",
            "반도체",
            "2차전지",
            "배터리",
            "바이오",
            "조선",
            "방산",
            "강세",
        ),
    ),
    _SectionRule(
        "disclosure_research",
        "공시/리포트",
        ("공시", "리포트", "목표가", "투자의견", "계약", "공급계약", "실적", "발표"),
    ),
    _SectionRule(
        "flow_watchlist",
        "수급/관심종목 영향",
        ("수급", "외국인", "기관", "순매수", "순매도", "관심종목", "거래대금"),
    ),
)

_CRYPTO_SECTION_TITLES = {
    "btc_eth_market": "BTC/ETH Market",
    "etf_institutional": "ETF / Institutional Flows",
    "regulation_policy": "Regulation / Policy",
    "security_defi": "Security / DeFi",
    "alt_sector": "Altcoins / Sector",
}
_CRYPTO_CATEGORY_TO_SECTION = {
    "market_price": "btc_eth_market",
    "etf_institutional": "etf_institutional",
    "regulation_policy": "regulation_policy",
    "security_risk": "security_defi",
    "stablecoin_defi": "security_defi",
}
_CRYPTO_SECTION_ORDER = (
    "btc_eth_market",
    "etf_institutional",
    "regulation_policy",
    "security_defi",
    "alt_sector",
)

_NOISE_TERMS = (
    "celebrity",
    "mansion",
    "renovation show",
    "streaming show",
    "wedding",
    "engagement",
)
_US_LOW_SIGNAL_TERMS = (
    "high-yield savings",
    "savings account",
    "savings interest",
    "apy",
    "mortgage rates",
    "home buyers",
    "home-buying",
    "streaming in",
    "netflix",
    "hulu",
    "hbo max",
)
_US_HIGH_SIGNAL_TERMS = (
    "fed",
    "fomc",
    "cpi",
    "inflation",
    "treasury",
    "s&p",
    "s&p 500",
    "nasdaq",
    "dow",
    "earnings",
    "guidance",
    "stock",
    "stocks",
    "shares",
    "analyst",
    "upgrade",
    "downgrade",
)
_US_FEED_SOURCE_SECTION_HINTS = {
    "rss_cnbc_earnings": ("earnings", "Earnings / Guidance", 64),
    "rss_cnbc_finance": ("finance_credit_rates", "Finance / Credit / Rates", 60),
}
_US_EXPERIMENTAL_FEED_SOURCES = {
    "http_finviz_news",
    "rss_investing_stock_market_news",
}


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _article_text(article: Any) -> tuple[str, str]:
    title = str(_field(article, "title") or "")
    summary = str(_field(article, "summary") or "")
    keywords = _field(article, "keywords") or []
    keyword_text = " ".join(str(keyword) for keyword in keywords)
    return title.lower(), f"{title} {summary} {keyword_text}".lower()


def _low_signal_us_noise_hit(article: Any) -> bool:
    if _field(article, "stock_symbol"):
        return False
    title, full_text = _article_text(article)
    if not any(term in full_text for term in _US_LOW_SIGNAL_TERMS):
        return False
    return not any(term in title for term in _US_HIGH_SIGNAL_TERMS)


def _low_market_relevance() -> BriefingRelevance:
    return BriefingRelevance(
        score=0,
        section_id=None,
        section_title=None,
        include_in_briefing=False,
        matched_terms=[],
        reason="low_market_relevance",
    )


def _score_rule(article: Any, rules: tuple[_SectionRule, ...]) -> BriefingRelevance:
    title, full_text = _article_text(article)
    scored: list[tuple[int, _SectionRule, list[str]]] = []

    for rule in rules:
        matched = [term for term in rule.terms if term in full_text]
        if not matched:
            continue
        score = min(90, 20 + len(matched) * 12)
        score += min(25, sum(10 for term in matched if term in title))
        if _field(article, "stock_symbol"):
            score += 8
        scored.append((min(100, score), rule, matched))

    if not scored:
        noise_hit = any(term in full_text for term in _NOISE_TERMS)
        reason = "low_market_relevance" if noise_hit else "uncategorized_market_news"
        return BriefingRelevance(
            score=0,
            section_id=None,
            section_title=None,
            include_in_briefing=False,
            matched_terms=[],
            reason=reason,
        )

    score, rule, matched_terms = max(scored, key=lambda item: item[0])
    return BriefingRelevance(
        score=score,
        section_id=rule.section_id,
        section_title=rule.title,
        include_in_briefing=score >= 40,
        matched_terms=sorted(set(matched_terms)),
        reason=None if score >= 40 else "low_market_relevance",
    )


def _apply_us_feed_source_hints(
    article: Any,
    relevance: BriefingRelevance,
) -> BriefingRelevance:
    feed_source = str(_field(article, "feed_source") or "")
    source_hint = f"feed_source:{feed_source}" if feed_source else ""

    if feed_source in _US_FEED_SOURCE_SECTION_HINTS:
        section_id, section_title, minimum_score = _US_FEED_SOURCE_SECTION_HINTS[
            feed_source
        ]
        matched_terms = list(relevance.matched_terms)
        if source_hint:
            matched_terms.append(source_hint)
        return BriefingRelevance(
            score=max(relevance.score, minimum_score),
            section_id=section_id,
            section_title=section_title,
            include_in_briefing=True,
            matched_terms=sorted(set(matched_terms)),
            reason=None,
        )

    if feed_source in _US_EXPERIMENTAL_FEED_SOURCES and relevance.include_in_briefing:
        matched_terms = list(relevance.matched_terms)
        if source_hint:
            matched_terms.append(source_hint)
        return BriefingRelevance(
            score=min(100, relevance.score + 4),
            section_id=relevance.section_id,
            section_title=relevance.section_title,
            include_in_briefing=relevance.include_in_briefing,
            matched_terms=sorted(set(matched_terms)),
            reason=relevance.reason,
        )

    return relevance


def _score_crypto(article: Any) -> BriefingRelevance:
    crypto = score_crypto_news_article(article)
    section_id = _CRYPTO_CATEGORY_TO_SECTION.get(crypto.category or "")
    if crypto.include_in_briefing and section_id is None:
        section_id = "alt_sector"
    section_title = _CRYPTO_SECTION_TITLES.get(section_id or "") if section_id else None
    return BriefingRelevance(
        score=crypto.score,
        section_id=section_id,
        section_title=section_title,
        include_in_briefing=crypto.include_in_briefing and section_id is not None,
        matched_terms=crypto.matched_terms,
        reason=crypto.noise_reason,
    )


def _rules_for_market(market: str) -> tuple[_SectionRule, ...]:
    if market == "us":
        return _US_RULES
    if market == "kr":
        return _KR_RULES
    return ()


def _section_order_for_market(market: str) -> list[str]:
    if market == "crypto":
        return list(_CRYPTO_SECTION_ORDER)
    return [rule.section_id for rule in _rules_for_market(market)]


def _score_article(article: Any, market: str) -> BriefingRelevance:
    if market == "crypto":
        return _score_crypto(article)
    if market == "us" and _low_signal_us_noise_hit(article):
        return _low_market_relevance()
    relevance = _score_rule(article, _rules_for_market(market))
    if market == "us":
        return _apply_us_feed_source_hints(article, relevance)
    return relevance


def format_market_news_briefing(
    articles: list[Any],
    *,
    market: str | None,
    limit: int | None = None,
) -> MarketNewsBriefing:
    """Format recent raw market-news rows into briefing sections.

    This is intentionally read-layer only. It groups/ranks already stored articles for
    user-facing briefings without changing raw-news ingestion or persistence.
    """

    normalized_market = (market or "").lower()
    scored = [
        BriefingItem(
            article=article, relevance=_score_article(article, normalized_market)
        )
        for article in articles
    ]
    included = [item for item in scored if item.relevance.include_in_briefing]
    excluded = [item for item in scored if not item.relevance.include_in_briefing]

    grouped: dict[str, list[BriefingItem]] = {}
    for item in included:
        if item.relevance.section_id is None:
            excluded.append(item)
            continue
        grouped.setdefault(item.relevance.section_id, []).append(item)

    for items in grouped.values():
        items.sort(key=lambda item: item.relevance.score, reverse=True)

    remaining = limit if limit is not None and limit >= 0 else None
    sections: list[BriefingSection] = []
    for section_id in _section_order_for_market(normalized_market):
        items = grouped.get(section_id, [])
        if not items:
            continue
        if remaining is not None:
            if remaining <= 0:
                break
            items = items[:remaining]
            remaining -= len(items)
        title = items[0].relevance.section_title or section_id
        sections.append(
            BriefingSection(section_id=section_id, title=title, items=items)
        )

    summary = {
        "included": sum(len(section.items) for section in sections),
        "excluded": len(excluded),
        "sections": len(sections),
        "uncategorized": sum(
            1
            for item in excluded
            if item.relevance.reason == "uncategorized_market_news"
        ),
    }
    excluded.sort(key=lambda item: item.relevance.score, reverse=True)
    return MarketNewsBriefing(
        market=normalized_market,
        sections=sections,
        excluded=excluded,
        summary=summary,
    )
