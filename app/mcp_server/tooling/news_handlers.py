from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive
from app.models.news import NewsArticle
from app.services.llm_news_service import get_news_articles

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "search_news"]


def _article_to_dict(article: NewsArticle) -> dict[str, Any]:
    return {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "feed_source": article.feed_source,
        "summary": article.summary,
        "published_at": article.article_published_at.isoformat()
        if article.article_published_at
        else None,
        "keywords": article.keywords,
        "stock_symbol": article.stock_symbol,
        "stock_name": article.stock_name,
    }


async def _get_market_news_impl(
    hours: int | None = 24,
    feed_source: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    limit: int | None = 20,
) -> dict[str, Any]:
    hours = hours or 24
    limit = limit or 20

    articles, total = await get_news_articles(
        hours=hours,
        feed_source=feed_source,
        source=source,
        keyword=keyword,
        limit=limit,
    )

    news_list = [_article_to_dict(a) for a in articles]
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    return {
        "count": len(news_list),
        "total": total,
        "news": news_list,
        "sources": sorted(source_names),
        "feed_sources": sorted(feed_source_names),
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
            "Get recent market news. Supports filtering by publisher (source), "
            "collection path (feed_source), and keyword. Returns both publisher names "
            "and collection paths for briefing segmentation."
        ),
    )
    async def get_market_news(
        hours: int = 24,
        feed_source: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await _get_market_news_impl(
            hours=hours,
            feed_source=feed_source,
            source=source,
            keyword=keyword,
            limit=limit,
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
