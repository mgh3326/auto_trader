from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.news import NewsArticle
from app.services.crypto_news_relevance_service import (
    rank_crypto_news_for_briefing,
    score_crypto_news_article,
)
from app.services.kr_news_symbol_mapping.contract import CandidateRow, MappedSymbol
from app.services.kr_news_symbol_mapping.related_lookup import (
    load_related_rows_by_article_ids,
)
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.llm_news_service import get_news_articles
from app.services.market_news_briefing_formatter import (
    BriefingSection,
    format_market_news_briefing,
)
from app.services.news_entity_matcher import match_symbols_for_article

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues"]


def _mapped_symbol_to_dict(symbol: MappedSymbol) -> dict[str, Any]:
    return {
        "symbol": symbol.symbol,
        "market": symbol.market,
        "mapping_source": symbol.mapping_source,
        "confidence": symbol.confidence,
        "is_primary": symbol.is_primary,
        "matched_term": symbol.matched_term,
    }


def compute_mapped_symbols(
    article: NewsArticle, related_rows: tuple[CandidateRow, ...]
) -> list[dict[str, Any]]:
    """Per-article symbol mapping for market news: persisted related rows + live NER,
    resolved by the shared resolver (naver_code > candidate > ner). [] if no match."""
    market = article.market if isinstance(article.market, str) else "kr"
    stock_symbol = article.stock_symbol if isinstance(article.stock_symbol, str) else None
    title = article.title if isinstance(article.title, str) else None
    summary = article.summary if isinstance(article.summary, str) else None
    keywords = article.keywords if isinstance(article.keywords, (list, tuple)) else []

    ner_matches = match_symbols_for_article(
        title=title,
        summary=summary,
        keywords=keywords,
        market=market,
    )
    mapped = resolve_article_symbols(
        market=market,
        stock_symbol=stock_symbol,
        related_rows=related_rows,
        ner_matches=ner_matches,
    )
    return [_mapped_symbol_to_dict(m) for m in mapped]


def _article_to_dict(
    article: NewsArticle,
    *,
    include_crypto_relevance: bool = False,
    briefing_relevance: dict[str, Any] | None = None,
    mapped_by_id: dict[int, list[dict[str, Any]]] | None = None,
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
        "mapped_symbols": (mapped_by_id or {}).get(article.id, []),
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
    mapped_by_id: dict[int, list[dict[str, Any]]] | None = None,
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
                    mapped_by_id=mapped_by_id,
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

    related_by_id = await load_related_rows_by_article_ids([a.id for a in articles])
    mapped_by_id: dict[int, list[dict[str, Any]]] = {
        a.id: compute_mapped_symbols(a, related_by_id.get(a.id, ())) for a in articles
    }

    excluded_news: list[dict[str, Any]] = []
    briefing_summary = None
    briefing_sections: list[dict[str, Any]] = []
    if market == "crypto":
        if briefing_filter:
            ranking = rank_crypto_news_for_briefing(list(articles), limit=limit)
            news_list = [
                _article_to_dict(
                    item.article,
                    include_crypto_relevance=True,
                    mapped_by_id=mapped_by_id,
                )
                for item in ranking.included
            ]
            excluded_news = [
                _article_to_dict(
                    item.article,
                    include_crypto_relevance=True,
                    mapped_by_id=mapped_by_id,
                )
                for item in ranking.excluded
            ]
            briefing_summary = ranking.summary
            briefing = format_market_news_briefing(
                list(articles), market=market, limit=limit
            )
            briefing_sections = _briefing_sections_to_dict(
                briefing.sections,
                include_crypto_relevance=True,
                mapped_by_id=mapped_by_id,
            )
        else:
            news_list = [
                _article_to_dict(
                    a, include_crypto_relevance=True, mapped_by_id=mapped_by_id
                )
                for a in articles
            ]
    elif briefing_filter and market in {"us", "kr"}:
        briefing = format_market_news_briefing(
            list(articles), market=market, limit=limit
        )
        news_list = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
                mapped_by_id=mapped_by_id,
            )
            for section in briefing.sections
            for item in section.items
        ]
        excluded_news = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
                mapped_by_id=mapped_by_id,
            )
            for item in briefing.excluded
        ]
        briefing_summary = briefing.summary
        briefing_sections = _briefing_sections_to_dict(
            briefing.sections, mapped_by_id=mapped_by_id
        )
    else:
        news_list = [_article_to_dict(a, mapped_by_id=mapped_by_id) for a in articles]
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    return {
        "surface": "legacy_market_briefing",
        "advisory": (
            "Legacy broad-market DB-backed surface for briefing only; "
            "NOT investment-decision evidence. Use get_news for symbol-level decisions."
        ),
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


def _register_news_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_market_news",
        description=(
            "[LEGACY: broad market DB-backed briefing surface; NOT investment-decision "
            "evidence — use get_news for symbol-level decisions] "
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
        name="get_market_issues",
        description=(
            "Read-only deterministic market issue clusters from collected news "
            "(ROB-130). Groups recent articles by entity/topic and ranks by "
            "recency + source diversity + mention count."
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
