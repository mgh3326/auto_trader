# app/services/kr_news_symbol_mapping/db_provider.py
"""DB-backed ArticleProvider for the kr_news_symbol_mapping read-model (ROB-398).

Adapts get_news_articles_with_fallback (exact->related->alias) + a separate
news_article_related_symbols lookup into ArticleView[] for get_symbol_news_mapping.
Read-only; self-acquires sessions; no writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping.contract import ArticleView, CandidateRow
from app.services.llm_news_service import get_news_articles_with_fallback


def _candidate_rows_from_orm(
    rows: Sequence[NewsArticleRelatedSymbol],
) -> tuple[CandidateRow, ...]:
    return tuple(
        CandidateRow(
            symbol=r.symbol,
            source=r.source,
            score=r.score,
            rank=r.rank,
            matched_term=r.matched_term,
        )
        for r in rows
    )


def _article_to_view(
    article: NewsArticle, related_rows: tuple[CandidateRow, ...]
) -> ArticleView:
    return ArticleView(
        market=article.market,
        stock_symbol=article.stock_symbol,
        related_rows=related_rows,
        title=article.title,
        summary=article.summary,
        keywords=tuple(article.keywords or ()),
        as_of=article.article_published_at or article.scraped_at,
        url=article.url,
    )


async def _load_related_rows(
    article_ids: Sequence[int],
) -> dict[int, tuple[CandidateRow, ...]]:
    """Load news_article_related_symbols for the given article ids (own session,

    avoids DetachedInstanceError from lazy article.related_symbols).
    """
    if not article_ids:
        return {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id.in_(list(article_ids))
            )
        )
        rows = result.scalars().all()
    grouped: dict[int, list[NewsArticleRelatedSymbol]] = {}
    for row in rows:
        grouped.setdefault(row.article_id, []).append(row)
    return {aid: _candidate_rows_from_orm(rs) for aid, rs in grouped.items()}


async def db_article_provider(
    symbol: str, market: str, hours: int, limit: int
) -> list[ArticleView]:
    """ArticleProvider: symbol-targeted articles (exact->related->alias) mapped to

    ArticleView with per-article related_symbols. Positional signature matches the
    ArticleProvider contract; fail-open is the caller's concern (returns [] if empty).
    """
    lookup = await get_news_articles_with_fallback(
        symbol=symbol, market=market, hours=hours, limit=limit
    )
    if not lookup.articles:
        return []
    related = await _load_related_rows([a.id for a in lookup.articles])
    return [_article_to_view(a, related.get(a.id, ())) for a in lookup.articles]
