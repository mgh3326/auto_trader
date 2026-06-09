"""Batch loader: news_article_related_symbols grouped by article_id (ROB-398).

Shared, read-only, self-acquires session. Avoids DetachedInstanceError from lazy
article.related_symbols when callers hold detached NewsArticle objects."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.news import NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping.contract import CandidateRow


def _group_rows(
    rows: Sequence[NewsArticleRelatedSymbol],
) -> dict[int, tuple[CandidateRow, ...]]:
    grouped: dict[int, list[CandidateRow]] = {}
    for row in rows:
        grouped.setdefault(row.article_id, []).append(
            CandidateRow(
                symbol=row.symbol,
                source=row.source,
                score=row.score,
                rank=row.rank,
                matched_term=row.matched_term,
            )
        )
    return {aid: tuple(rs) for aid, rs in grouped.items()}


async def load_related_rows_by_article_ids(
    article_ids: Sequence[int],
) -> dict[int, tuple[CandidateRow, ...]]:
    if not article_ids:
        return {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id.in_(list(article_ids))
            )
        )
        rows = result.scalars().all()
    return _group_rows(rows)
