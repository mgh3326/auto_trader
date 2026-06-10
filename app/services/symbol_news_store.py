"""Persistence seam for the symbol-news relevance lifecycle (ROB-491).

All DB writes for the get_news cache go through here: ① article/link upsert at
fetch time (set-difference by unique url — feed order is never trusted), and
② judgment apply via the token-authed ingest route (PR2). No MCP imports, no
LLM, no broker/order surface. Callers own session lifecycle and commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.symbol_news_relevance import build_relevance_hints

logger = logging.getLogger(__name__)

KR_FEED_SOURCE = "naver_item_news"


@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class StoredSymbolNews:
    article_id: int
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    relevance: dict[str, Any]


def _utcnow() -> datetime:
    # Convention in this repo: naive UTC for DB storage to avoid asyncpg DataError
    return datetime.now(tz=UTC).replace(tzinfo=None)


def derive_status(relationship: str, relevance: str) -> str:
    """Server-owned status rule — the judgment job never writes status itself."""
    if relationship == "unrelated" or relevance == "low":
        return "excluded"
    return "confirmed"


def _relevance_block(link: SymbolNewsRelevance) -> dict[str, Any]:
    return {
        "status": link.status,
        "relationship": link.relationship,
        "relevance": link.relevance,
        "price_relevance": link.price_relevance,
        "score": link.score,
        "reason": link.reason,
        "judged_by": link.judged_by,
        "judged_at": link.judged_at.isoformat() if link.judged_at else None,
        "hints": link.hints,
    }


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> None:
    """Set-difference upsert: new urls insert, known urls no-op (idempotent)."""
    if not items:
        return
    now = _utcnow()
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "market": "kr",
            "feed_source": feed_source,
            "article_published_at": item.published_at.replace(tzinfo=None)
            if item.published_at
            else None,
            "is_analyzed": False,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for item in items
    ]
    await db.execute(
        pg_insert(NewsArticle)
        .values(article_values)
        .on_conflict_do_nothing(index_elements=[NewsArticle.url])
    )
    urls = [item.url for item in items]
    id_rows = await db.execute(
        select(NewsArticle.id, NewsArticle.url).where(NewsArticle.url.in_(urls))
    )
    url_to_id = {url: article_id for article_id, url in id_rows.all()}

    link_values = []
    for item in items:
        article_id = url_to_id.get(item.url)
        if (
            article_id is None
        ):  # insert race lost and url missing — skip, next call heals
            continue
        link_values.append(
            {
                "article_id": article_id,
                "market": "kr",
                "symbol": symbol,
                "feed_source": feed_source,
                "first_seen_at": now,
                "status": "pending",
                "hints": build_relevance_hints(
                    symbol=symbol, market="kr", title=item.title
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
    if link_values:
        await db.execute(
            pg_insert(SymbolNewsRelevance)
            .values(link_values)
            .on_conflict_do_nothing(
                index_elements=[
                    SymbolNewsRelevance.article_id,
                    SymbolNewsRelevance.market,
                    SymbolNewsRelevance.symbol,
                ]
            )
        )
    await db.commit()


async def load_symbol_news(
    db: AsyncSession,
    symbol: str,
    market: str,
    limit: int,
) -> tuple[list[StoredSymbolNews], int]:
    """Canonical read: non-excluded rows newest-first + excluded count."""
    rows = await db.execute(
        select(NewsArticle, SymbolNewsRelevance)
        .join(
            SymbolNewsRelevance,
            SymbolNewsRelevance.article_id == NewsArticle.id,
        )
        .where(
            SymbolNewsRelevance.market == market,
            SymbolNewsRelevance.symbol == symbol,
            SymbolNewsRelevance.status != "excluded",
        )
        .order_by(
            NewsArticle.article_published_at.desc().nullslast(),
            NewsArticle.id.desc(),
        )
        .limit(limit)
    )
    stored = [
        StoredSymbolNews(
            article_id=article.id,
            url=article.url,
            title=article.title,
            source=article.source,
            published_at=article.article_published_at,
            relevance=_relevance_block(link),
        )
        for article, link in rows.all()
    ]
    excluded_count = (
        await db.execute(
            select(func.count())
            .select_from(SymbolNewsRelevance)
            .where(
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
                SymbolNewsRelevance.status == "excluded",
            )
        )
    ).scalar_one()
    return stored, int(excluded_count)
