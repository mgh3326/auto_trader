from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive
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

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "search_news"]


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
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    return {
        "market": market,
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


async def _search_news_db(
    query: str,
    days: int = 7,
    limit: int = 20,
) -> tuple[list[NewsArticle], int]:
    cutoff = now_kst_naive() - timedelta(days=days)
    like_pattern = f"%{query}%"

    async with AsyncSessionLocal() as db:
        base_filter = [
            NewsArticle.article_published_at >= cutoff,
            or_(
                NewsArticle.title.ilike(like_pattern),
                NewsArticle.keywords.op("@>")(cast(json.dumps([query]), JSONB)),
            ),
        ]

        q = (
            select(NewsArticle)
            .where(*base_filter)
            .order_by(NewsArticle.article_published_at.desc().nulls_last())
            .limit(limit)
        )
        result = await db.execute(q)
        articles = list(result.scalars().all())

        count_q = select(func.count(NewsArticle.id)).where(*base_filter)
        count_result = await db.execute(count_q)
        total = count_result.scalar_one()

    return articles, total


async def _search_news_impl(
    query: str,
    days: int | None = 7,
    limit: int | None = 20,
) -> dict[str, Any]:
    days = days or 7
    limit = limit or 20

    articles, total = await _search_news_db(query=query, days=days, limit=limit)
    news_list = [_article_to_dict(a) for a in articles]

    return {
        "query": query,
        "count": len(news_list),
        "total": total,
        "news": news_list,
    }


def _register_news_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_market_news",
        description=(
            "Get recent market news. Supports filtering by market, publisher (source), "
            "collection path (feed_source), and keyword. Returns both publisher names "
            "and collection paths for briefing segmentation. briefing_filter=True "
            "formats market-specific sections for kr/us and ranks crypto-relevant "
            "items while separating broad-tech noise."
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
        name="search_news",
        description="Search news articles by keyword. Searches title and keywords field.",
    )
    async def search_news(
        query: str,
        days: int = 7,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await _search_news_impl(query=query, days=days, limit=limit)
