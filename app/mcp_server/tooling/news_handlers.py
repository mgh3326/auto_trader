from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.news import NewsArticle
from app.services.crypto_news_relevance_service import (
    rank_crypto_news_for_briefing,
    score_crypto_news_article,
)
from app.services.llm_news_service import get_news_articles
from app.services.market_news_briefing_formatter import (
    BriefingSection,
    format_market_news_briefing,
)
from app.services.market_news_noise import classify_title_noise, noise_reason

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues"]


def _article_to_dict(
    article: NewsArticle,
    *,
    include_crypto_relevance: bool = False,
    briefing_relevance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "feed_source": article.feed_source,
        "market": article.market,
        "summary": article.summary,
        "published_at": article.article_published_at.isoformat()
        if article.article_published_at
        else None,
        "keywords": article.keywords,
        "stock_symbol": article.stock_symbol,
        "stock_name": article.stock_name,
    }
    if include_crypto_relevance:
        item["crypto_relevance"] = score_crypto_news_article(article).as_dict()
    if briefing_relevance is not None:
        item["briefing_relevance"] = briefing_relevance
    return item


def _briefing_sections_to_dict(
    sections: list[BriefingSection],
    *,
    include_crypto_relevance: bool = False,
) -> list[dict[str, Any]]:
    return [
        {
            "section_id": section.section_id,
            "title": section.title,
            "count": len(section.items),
            "items": [
                _article_to_dict(
                    item.article,
                    include_crypto_relevance=include_crypto_relevance,
                    briefing_relevance=item.relevance.as_dict(),
                )
                for item in section.items
            ],
        }
        for section in sections
    ]


async def _get_market_news_impl(
    market: str | None = None,
    hours: int | None = 24,
    feed_source: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    limit: int | None = 20,
    briefing_filter: bool = False,
) -> dict[str, Any]:
    hours = hours or 24
    limit = limit or 20

    query_limit = limit
    if market in {"crypto", "us", "kr"} and briefing_filter:
        # Pull a slightly larger window so ranking can hide low-signal noise
        # without returning an under-filled briefing when relevant items exist.
        query_limit = max(limit * 3, limit)

    articles, total = await get_news_articles(
        market=market,
        hours=hours,
        feed_source=feed_source,
        source=source,
        keyword=keyword,
        limit=query_limit,
    )

    # ROB-502 quality gate (always on): noise-classified titles never reach
    # the default list — they move to excluded_news with an explicit reason.
    gated_articles = []
    noise_excluded: list[dict[str, Any]] = []
    for article in articles:
        noise = classify_title_noise(article.title or "")
        if noise:
            item = _article_to_dict(article)
            item["excluded_reason"] = noise_reason(noise)
            noise_excluded.append(item)
        else:
            gated_articles.append(article)
    articles = gated_articles

    excluded_news: list[dict[str, Any]] = []
    briefing_summary = None
    briefing_sections: list[dict[str, Any]] = []
    if market == "crypto":
        if briefing_filter:
            ranking = rank_crypto_news_for_briefing(list(articles), limit=limit)
            news_list = [
                _article_to_dict(item.article, include_crypto_relevance=True)
                for item in ranking.included
            ]
            excluded_news = [
                _article_to_dict(item.article, include_crypto_relevance=True)
                for item in ranking.excluded
            ]
            briefing_summary = ranking.summary
            briefing = format_market_news_briefing(
                list(articles), market=market, limit=limit
            )
            briefing_sections = _briefing_sections_to_dict(
                briefing.sections, include_crypto_relevance=True
            )
        else:
            news_list = [
                _article_to_dict(a, include_crypto_relevance=True) for a in articles
            ]
    elif briefing_filter and market in {"us", "kr"}:
        briefing = format_market_news_briefing(
            list(articles), market=market, limit=limit
        )
        news_list = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
            )
            for section in briefing.sections
            for item in section.items
        ]
        excluded_news = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
            )
            for item in briefing.excluded
        ]
        briefing_summary = briefing.summary
        briefing_sections = _briefing_sections_to_dict(briefing.sections)
    else:
        news_list = [_article_to_dict(a) for a in articles]
    excluded_news = noise_excluded + excluded_news
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    # ROB-502: degraded states are explicit — no filler when nothing passes.
    status = "ok"
    degraded_reason = None
    if total == 0:
        status = "no_recent_articles"
        degraded_reason = (
            f"no articles in the last {hours}h window — "
            "ingestion may be stale or paused"
        )
    elif not news_list:
        status = "no_meaningful_items"
        degraded_reason = (
            f"{total} article(s) in window, but none passed the quality gate "
            f"({len(excluded_news)} excluded — see excluded_news reasons); "
            "no filler is generated"
        )

    return {
        "surface": "quality_gated_market_briefing",
        "advisory": (
            "Quality-gated broad-market DB-backed surface for briefing only; "
            "NOT investment-decision evidence. Use get_news for symbol-level "
            "decisions. Noise-classified items appear in excluded_news with "
            "reasons instead of the main list."
        ),
        "market": market,
        "status": status,
        "degraded_reason": degraded_reason,
        "count": len(news_list),
        "total": total,
        "news": news_list,
        "sources": sorted(source_names),
        "feed_sources": sorted(feed_source_names),
        "briefing_filter": bool(briefing_filter),
        "briefing_summary": briefing_summary,
        "briefing_sections": briefing_sections,
        "excluded_news": excluded_news,
    }


def _register_news_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_market_news",
        description=(
            "[Quality-gated broad market briefing surface; NOT investment-decision "
            "evidence — use get_news for symbol-level decisions] "
            "Get recent market news with a noise gate always on (ROB-502): "
            "personal-finance/lifestyle/sponsored/price-prediction/broad-tech items "
            "move to excluded_news with an excluded_reason instead of the main list. "
            "status is 'ok' | 'no_meaningful_items' | 'no_recent_articles' with "
            "degraded_reason — no filler is generated. Supports filtering by market, "
            "publisher (source), collection path (feed_source), and keyword. "
            "briefing_filter=True additionally formats market-specific sections for "
            "kr/us and ranks crypto-relevant items."
        ),
    )
    async def get_market_news(
        market: str | None = None,
        hours: int = 24,
        feed_source: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        briefing_filter: bool = False,
    ) -> dict[str, Any]:
        return await _get_market_news_impl(
            market=market,
            hours=hours,
            feed_source=feed_source,
            source=source,
            keyword=keyword,
            limit=limit,
            briefing_filter=briefing_filter,
        )

    @mcp.tool(
        name="get_market_issues",
        description=(
            "Read-only deterministic market issue clusters from collected news "
            "(ROB-130, quality-gated per ROB-502). Groups recent articles by "
            "entity/topic, merges near-duplicate syndicated stories, and ranks by "
            "recency + source diversity + mention count. Noise-classified articles "
            "never enter clustering, and thin clusters (single article AND single "
            "source, non-official feed) are withheld. status/degraded_reason/"
            "quality_gate report what the gate did; empty results are explicit "
            "(no_meaningful_items), never filler."
        ),
    )
    async def get_market_issues(
        market: str = "all",
        window_hours: int = 24,
        limit: int = 20,
    ) -> dict[str, Any]:
        from app.services.news_issue_clustering_service import build_market_issues

        response = await build_market_issues(
            market=market, window_hours=window_hours, limit=limit
        )
        return response.model_dump(mode="json")
