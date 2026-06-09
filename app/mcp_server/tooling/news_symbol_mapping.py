# app/mcp_server/tooling/news_symbol_mapping.py
"""MCP handler for get_symbol_news_mapping (ROB-398 surface slice 1).

Exposes the news-symbol mapping read-model: symbol -> mapped news (symbol /
mapping_source / confidence / is_primary) + url + as_of, with honest data_state.
Read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.kr_news_symbol_mapping.contract import SymbolNewsMapping
from app.services.kr_news_symbol_mapping.db_provider import db_article_provider
from app.services.kr_news_symbol_mapping.query_service import (
    ArticleProvider,
    get_symbol_news_mapping,
)


def _format_symbol_news_mapping(mapping: SymbolNewsMapping) -> dict[str, Any]:
    data_state = mapping.freshness.overall
    articles = [
        {
            "title": a.title,
            "url": a.url,
            "summary": a.summary,
            "as_of": a.as_of.isoformat() if a.as_of else None,
            "mapped_symbols": [
                {
                    "symbol": s.symbol,
                    "market": s.market,
                    "mapping_source": s.mapping_source,
                    "confidence": s.confidence,
                    "is_primary": s.is_primary,
                    "matched_term": s.matched_term,
                }
                for s in a.mapped_symbols
            ],
        }
        for a in mapping.articles
    ]
    warnings: list[str] = []
    if data_state == "unavailable":
        warnings.append("해당 종목에 매핑된 뉴스가 없습니다 (최근 윈도우 내).")
    elif data_state == "stale":
        warnings.append("매핑된 뉴스가 오래되었습니다 — 신선도에 주의하세요.")
    return {
        "symbol": mapping.symbol,
        "market": mapping.market,
        "data_state": data_state,
        "latest_as_of": (
            mapping.freshness.latest_as_of.isoformat()
            if mapping.freshness.latest_as_of
            else None
        ),
        "articles": articles,
        "warnings": warnings,
    }


async def handle_get_symbol_news_mapping(
    *,
    symbol: str,
    market: str = "kr",
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
    article_provider: ArticleProvider | None = None,
) -> dict[str, Any]:
    mapping = await get_symbol_news_mapping(
        symbol,
        market=market,
        hours=hours,
        limit=limit,
        now=now,
        article_provider=article_provider or db_article_provider,
    )
    return _format_symbol_news_mapping(mapping)
