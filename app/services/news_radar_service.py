# app/services/news_radar_service.py
"""Build the read-only Market Risk News Radar response (ROB-109).

Read-only. No DB writes. No broker calls. No mutation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

from app.schemas.news import NewsReadinessResponse, NewsSourceCoverage
from app.schemas.news_radar import (
    NewsRadarItem,
    NewsRadarMarket,
    NewsRadarReadiness,
    NewsRadarReadinessStatus,
    NewsRadarResponse,
    NewsRadarRiskCategory,
    NewsRadarSection,
    NewsRadarSeverity,
    NewsRadarSourceCoverage,
    NewsRadarSummary,
)
from app.services.llm_news_service import (
    get_news_articles,
    get_news_readiness,
)
from app.services.market_news_briefing_formatter import (
    format_market_news_briefing,
)
from app.services.news_radar_classifier import (
    NewsRadarItemClassification,
    classify_news_radar_item,
)

_SECTION_TITLES: dict[NewsRadarRiskCategory, str] = {
    "geopolitical_oil": "Geopolitical / Oil shock",
    "macro_policy": "Macro / Policy",
    "crypto_security": "Crypto / Security",
    "earnings_bigtech": "Earnings / Big tech",
    "korea_market": "Korea market",
}
_SECTION_ORDER: tuple[NewsRadarRiskCategory, ...] = (
    "geopolitical_oil",
    "macro_policy",
    "crypto_security",
    "earnings_bigtech",
    "korea_market",
)
_BRIEFING_INCLUDE_THRESHOLD = 40
_AGGREGATE_MARKETS = ("kr", "us", "crypto")
_MAX_INTERNAL_FETCH_LIMIT = 500
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _plain_text(value: Any, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    text = unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if max_length is not None and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _field(article: Any, name: str) -> Any:
    if isinstance(article, dict):
        return article.get(name)
    return getattr(article, name, None)


def _matches_query(article: Any, q: str | None) -> bool:
    if not q:
        return True
    needle = q.lower()
    title = str(_field(article, "title") or "").lower()
    summary = str(_field(article, "summary") or "").lower()
    return needle in title or needle in summary


def _briefing_score_lookup(
    articles: list[Any], market: str
) -> dict[int, tuple[int, bool]]:
    """Run the existing briefing scorer per market and return id -> (score, included)."""
    if market == "all":
        # Score per-article using the article's own market.
        scores: dict[int, tuple[int, bool]] = {}
        for article in articles:
            article_market = str(_field(article, "market") or "").lower()
            briefing = format_market_news_briefing(
                [article], market=article_market, limit=None
            )
            score, included = _extract_first_score(briefing)
            article_id = _field(article, "id")
            if article_id is not None:
                scores[int(article_id)] = (score, included)
        return scores

    briefing = format_market_news_briefing(articles, market=market, limit=None)
    scores = {}
    for section in briefing.sections:
        for item in section.items:
            article_id = _field(item.article, "id")
            if article_id is not None:
                scores[int(article_id)] = (item.relevance.score, True)
    for excluded_item in briefing.excluded:
        article_id = _field(excluded_item.article, "id")
        if article_id is not None and int(article_id) not in scores:
            scores[int(article_id)] = (excluded_item.relevance.score, False)
    return scores


def _extract_first_score(briefing: Any) -> tuple[int, bool]:
    for section in briefing.sections:
        if section.items:
            item = section.items[0]
            return (item.relevance.score, True)
    if briefing.excluded:
        item = briefing.excluded[0]
        return (item.relevance.score, False)
    return (0, False)


def _classification_to_item(
    article: Any,
    classification: NewsRadarItemClassification,
    *,
    briefing_score: int,
    included_in_briefing: bool,
) -> NewsRadarItem:
    article_id = _field(article, "id")
    symbol = _field(article, "stock_symbol")
    snippet = _plain_text(_field(article, "summary"), max_length=280)
    return NewsRadarItem(
        id=str(article_id) if article_id is not None else _field(article, "url") or "",
        title=_plain_text(_field(article, "title")) or "",
        source=_field(article, "source"),
        feed_source=_field(article, "feed_source"),
        url=str(_field(article, "url") or ""),
        published_at=_field(article, "article_published_at"),
        market=str(_field(article, "market") or ""),
        risk_category=classification.risk_category,
        severity=classification.severity,
        themes=classification.themes,
        symbols=[symbol] if isinstance(symbol, str) and symbol else [],
        included_in_briefing=included_in_briefing,
        briefing_reason=None
        if included_in_briefing
        else "filtered_out_low_rank_or_not_selected",
        briefing_score=briefing_score,
        snippet=snippet,
        matched_terms=classification.matched_terms,
    )


def _readiness_status(
    readiness: NewsReadinessResponse,
) -> NewsRadarReadinessStatus:
    if readiness.is_ready:
        return "ready"
    if readiness.is_stale and readiness.latest_finished_at is not None:
        return "stale"
    return "unavailable"


def _readiness_to_radar(
    readiness: NewsReadinessResponse,
    *,
    recent_6h_count: int,
    recent_24h_count: int,
) -> NewsRadarReadiness:
    return NewsRadarReadiness(
        status=_readiness_status(readiness),
        latest_scraped_at=readiness.latest_finished_at,
        latest_published_at=readiness.latest_article_published_at,
        recent_6h_count=recent_6h_count,
        recent_24h_count=recent_24h_count,
        source_count=len(readiness.source_counts),
        stale=readiness.is_stale,
        max_age_minutes=readiness.max_age_minutes,
        warnings=list(readiness.warnings),
    )


def _latest_datetime(values: list[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _prefix_source_coverage(
    market: str, coverage: list[NewsSourceCoverage]
) -> list[NewsSourceCoverage]:
    return [
        cov.model_copy(update={"feed_source": f"{market}:{cov.feed_source}"})
        for cov in coverage
    ]


def _aggregate_readiness(
    readiness_by_market: list[NewsReadinessResponse],
) -> NewsReadinessResponse:
    source_counts: dict[str, int] = {}
    source_coverage: list[NewsSourceCoverage] = []
    warnings: list[str] = []
    for readiness in readiness_by_market:
        market = readiness.market
        for source, count in readiness.source_counts.items():
            source_counts[f"{market}:{source}"] = count
        source_coverage.extend(
            _prefix_source_coverage(market, readiness.source_coverage or [])
        )
        warnings.extend(f"{market}: {warning}" for warning in readiness.warnings)
        if not readiness.is_ready:
            warnings.append(f"{market}: news readiness is not ready")

    return NewsReadinessResponse(
        market="all",
        is_ready=all(readiness.is_ready for readiness in readiness_by_market),
        is_stale=any(readiness.is_stale for readiness in readiness_by_market),
        latest_run_uuid=None,
        latest_status="success"
        if all(
            readiness.latest_status == "success" for readiness in readiness_by_market
        )
        else "partial",
        latest_finished_at=_latest_datetime(
            [readiness.latest_finished_at for readiness in readiness_by_market]
        ),
        latest_article_published_at=_latest_datetime(
            [readiness.latest_article_published_at for readiness in readiness_by_market]
        ),
        source_counts=source_counts,
        source_coverage=source_coverage,
        warnings=warnings,
        max_age_minutes=max(
            (readiness.max_age_minutes for readiness in readiness_by_market),
            default=180,
        ),
    )


async def _get_readiness_for_radar(
    market_filter: str | None,
) -> NewsReadinessResponse:
    if market_filter is not None:
        return await get_news_readiness(market=market_filter, max_age_minutes=180)

    return _aggregate_readiness(
        [
            await get_news_readiness(market=market, max_age_minutes=180)
            for market in _AGGREGATE_MARKETS
        ]
    )


def _build_sections(
    items: list[NewsRadarItem],
) -> list[NewsRadarSection]:
    grouped: dict[NewsRadarRiskCategory, list[NewsRadarItem]] = {}
    for item in items:
        if item.risk_category is None:
            continue
        grouped.setdefault(item.risk_category, []).append(item)

    sections: list[NewsRadarSection] = []
    for section_id in _SECTION_ORDER:
        bucket = grouped.get(section_id)
        if not bucket:
            continue
        bucket.sort(
            key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i.severity], i.title)
        )
        section_severity: NewsRadarSeverity = (
            "high"
            if any(it.severity == "high" for it in bucket)
            else "medium"
            if any(it.severity == "medium" for it in bucket)
            else "low"
        )
        sections.append(
            NewsRadarSection(
                section_id=section_id,
                title=_SECTION_TITLES[section_id],
                severity=section_severity,
                items=bucket,
            )
        )
    return sections


async def build_news_radar(
    *,
    market: NewsRadarMarket,
    hours: int,
    q: str | None,
    risk_category: NewsRadarRiskCategory | None,
    include_excluded: bool,
    limit: int,
) -> NewsRadarResponse:
    market_filter: str | None = None if market == "all" else market

    fetch_limit = min(max(limit * 5, limit), _MAX_INTERNAL_FETCH_LIMIT)
    articles, _total = await get_news_articles(
        market=market_filter,
        hours=hours,
        limit=fetch_limit,
        offset=0,
    )
    readiness = await _get_readiness_for_radar(market_filter)

    filtered_articles = [a for a in articles if _matches_query(a, q)]
    scores = _briefing_score_lookup(filtered_articles, market_filter or "all")

    items: list[NewsRadarItem] = []
    excluded_items: list[NewsRadarItem] = []
    for article in filtered_articles:
        article_id = _field(article, "id")
        score, included = scores.get(
            int(article_id) if article_id is not None else -1, (0, False)
        )
        included_in_briefing = included and score >= _BRIEFING_INCLUDE_THRESHOLD
        classification = classify_news_radar_item(article, briefing_score=score)
        if risk_category is not None and classification.risk_category != risk_category:
            continue
        radar_item = _classification_to_item(
            article,
            classification,
            briefing_score=score,
            included_in_briefing=included_in_briefing,
        )
        if included_in_briefing:
            items.append(radar_item)
            continue
        if include_excluded:
            items.append(radar_item)
            excluded_items.append(radar_item)

    items = items[:limit]
    excluded_items = excluded_items[:limit]
    sections = _build_sections(items)

    high_risk_count = sum(1 for i in items if i.severity == "high")
    included_count = sum(1 for i in items if i.included_in_briefing)
    excluded_count = sum(1 for i in items if not i.included_in_briefing)

    summary = NewsRadarSummary(
        high_risk_count=high_risk_count,
        total_count=len(items),
        included_in_briefing_count=included_count,
        excluded_but_collected_count=excluded_count,
    )

    coverage = [
        NewsRadarSourceCoverage(
            feed_source=cov.feed_source,
            recent_6h=cov.recent_6h,
            recent_24h=cov.recent_24h,
            latest_published_at=cov.latest_published_at,
            latest_scraped_at=cov.latest_scraped_at,
            status=cov.status,
        )
        for cov in (readiness.source_coverage or [])
    ]

    radar_readiness = _readiness_to_radar(
        readiness,
        recent_6h_count=sum(cov.recent_6h for cov in coverage),
        recent_24h_count=sum(cov.recent_24h for cov in coverage)
        or len(filtered_articles),
    )

    return NewsRadarResponse(
        market=market,
        as_of=datetime.now(tz=UTC),
        readiness=radar_readiness,
        summary=summary,
        sections=sections,
        items=items,
        excluded_items=excluded_items if include_excluded else [],
        source_coverage=coverage,
    )
