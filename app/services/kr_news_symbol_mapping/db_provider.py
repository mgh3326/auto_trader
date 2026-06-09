# app/services/kr_news_symbol_mapping/db_provider.py
"""DB-backed ArticleProvider for the kr_news_symbol_mapping read-model (ROB-398).

Adapts get_news_articles_with_fallback (exact->related->alias) + a separate
news_article_related_symbols lookup into ArticleView[] for get_symbol_news_mapping.
Read-only; self-acquires sessions; no writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping.contract import ArticleView, CandidateRow


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
