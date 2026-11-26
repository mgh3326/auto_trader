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
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.naver_news_urls import canonicalize_naver_news_url
from app.services.symbol_news_relevance import build_relevance_hints

logger = logging.getLogger(__name__)

KR_FEED_SOURCE = "naver_item_news"
FINNHUB_COMPANY_FEED_SOURCE = "finnhub_company_news"  # us
FINNHUB_GENERAL_FEED_SOURCE = "finnhub_general_news"  # crypto (심볼 키 아님)
_PUBLICATION_PRECISION_RANK = {"date": 1, "minute": 2, "second": 3}


@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    summary: str | None = None
    published_at_raw: str | None = None
    published_precision: str | None = None
    published_at_reason: str | None = None


@dataclass(frozen=True)
class StoredSymbolNews:
    article_id: int
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    relevance: dict[str, Any]
    summary: str | None = None
    # Original upstream acquisition time. ``news_articles.scraped_at`` is
    # insert-only for the URL-conflict path, so it preserves the first
    # trustworthy fetch instant instead of the time of a later failed retry.
    fetched_at: datetime | None = None


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


async def upsert_feed_articles(
    db: AsyncSession,
    market: str,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str,
) -> int:
    """Set-difference upsert: new urls insert, known urls no-op (idempotent).

    Returns the number of *newly created* pending links (ROB-506 enqueue
    trigger). 0 when every (article, symbol) pair already existed.
    """
    _articles_new, new_links = await _upsert_feed_articles(
        db, market, symbol, items, feed_source=feed_source
    )
    return new_links


async def _upsert_feed_articles(
    db: AsyncSession,
    market: str,
    symbol: str | None,
    items: list[FeedArticleInput],
    *,
    feed_source: str,
    canonical_dedupe: bool = False,
    commit: bool = True,
) -> tuple[int, int]:
    if not items:
        return 0, 0
    items_by_key: dict[str, FeedArticleInput] = {}
    candidate_urls: set[str] = set()
    candidate_prefixes: set[str] = set()
    if canonical_dedupe and market == "kr":
        # Resolve the collector's canonical identity against both the exact
        # incoming URL and the legacy Naver mobile URL before inserting. Keep
        # the provider URL in `news_articles.url`; get_news uses that exact key
        # to reconnect its live payload and freshness metadata.
        items_by_key: dict[str, FeedArticleInput] = {}
        for item in items:
            key = canonicalize_naver_news_url(item.url)
            items_by_key.setdefault(key, item)
        items = list(items_by_key.values())
        candidate_urls: set[str] = set()
        candidate_prefixes: set[str] = set()
        for item in items:
            canonical = canonicalize_naver_news_url(item.url)
            candidate_urls.add(item.url)
            candidate_urls.add(canonical)
            parts = urlsplit(canonical)
            if parts.hostname == "n.news.naver.com" and parts.path.startswith(
                "/article/"
            ):
                mobile_url = urlunsplit(
                    (parts.scheme, parts.netloc, "/mnews" + parts.path, "", "")
                )
                candidate_urls.add(mobile_url)
                candidate_prefixes.update({canonical, mobile_url})
        lookup_conditions = [NewsArticle.url.in_(candidate_urls)]
        lookup_conditions.extend(
            NewsArticle.url.startswith(prefix) for prefix in candidate_prefixes
        )
        prior_rows = await db.execute(
            select(
                NewsArticle.id,
                NewsArticle.url,
                NewsArticle.published_at_raw,
                NewsArticle.published_at,
                NewsArticle.published_precision,
            ).where(or_(*lookup_conditions))
        )
        existing_by_key: dict[
            str, tuple[int, str | None, datetime | None, str | None]
        ] = {}
        for article_id, url, raw, published_at, precision in prior_rows.all():
            try:
                key = canonicalize_naver_news_url(url)
            except ValueError:
                continue
            existing_by_key.setdefault(key, (article_id, raw, published_at, precision))
        # A canonical URL can already be stored under a provider URL from an
        # older collector. Enrich that row in place without changing its URL
        # key or first receipt time.
        for key, item in items_by_key.items():
            existing = existing_by_key.get(key)
            if existing is None:
                continue
            article_id, existing_raw, existing_published_at, existing_precision = (
                existing
            )
            incoming_published_at = item.published_at
            if (
                incoming_published_at is not None
                and incoming_published_at.utcoffset() is None
            ):
                incoming_published_at = incoming_published_at.replace(
                    tzinfo=ZoneInfo("Asia/Seoul")
                )
            existing_rank = _PUBLICATION_PRECISION_RANK.get(existing_precision or "", 0)
            incoming_rank = _PUBLICATION_PRECISION_RANK.get(
                item.published_precision or "", 0
            )
            values: dict[str, Any] = {}
            if incoming_rank > existing_rank:
                if item.published_at_raw is not None:
                    values["published_at_raw"] = item.published_at_raw
                if incoming_published_at is not None:
                    values["published_at"] = incoming_published_at
                if item.published_precision is not None:
                    values["published_precision"] = item.published_precision
                if item.published_at_reason is not None:
                    values["published_at_reason"] = item.published_at_reason
            else:
                if existing_raw is None and item.published_at_raw is not None:
                    values["published_at_raw"] = item.published_at_raw
                if existing_published_at is None and incoming_published_at is not None:
                    values["published_at"] = incoming_published_at
                if existing_precision is None and item.published_precision is not None:
                    values["published_precision"] = item.published_precision
            if values:
                await db.execute(
                    update(NewsArticle)
                    .where(NewsArticle.id == article_id)
                    .values(**values)
                )
        items = [
            item
            for item in items
            if canonicalize_naver_news_url(item.url) not in existing_by_key
        ]
    else:
        existing_by_key = {}
    if canonical_dedupe and market == "kr":
        # Items that were filtered above are represented by the original batch
        # when constructing links; the lookup includes all known URL variants.
        link_items = list(items_by_key.values())
    else:
        link_items = items
    now = _utcnow()
    now_aware = datetime.now(tz=UTC)
    publication_times = []
    for item in items:
        published_at = item.published_at
        if published_at is not None and published_at.utcoffset() is None:
            published_at = published_at.replace(
                tzinfo=ZoneInfo("Asia/Seoul") if market == "kr" else UTC
            )
        publication_times.append(published_at)
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "summary": item.summary,
            "market": market,
            "feed_source": feed_source,
            "article_published_at": item.published_at.replace(tzinfo=None)
            if item.published_at and item.published_at.utcoffset() is None
            else item.published_at.astimezone(ZoneInfo("Asia/Seoul")).replace(
                tzinfo=None
            )
            if item.published_at
            else None,
            "published_at_raw": item.published_at_raw,
            "published_at": published_at,
            "published_precision": item.published_precision,
            "published_at_reason": item.published_at_reason,
            "first_received_at": now_aware,
            "is_analyzed": False,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for item, published_at in zip(items, publication_times, strict=True)
    ]
    new_articles = 0
    if article_values:
        inserted_rows = await db.execute(
            pg_insert(NewsArticle)
            .values(article_values)
            .on_conflict_do_nothing(index_elements=[NewsArticle.url])
            .returning(NewsArticle.id)
        )
        new_articles = len(inserted_rows.all())
    if canonical_dedupe and market == "kr":
        candidate_urls.update(item.url for item in items)
        lookup_conditions = [NewsArticle.url.in_(candidate_urls)]
        lookup_conditions.extend(
            NewsArticle.url.startswith(prefix) for prefix in candidate_prefixes
        )
        id_rows = await db.execute(
            select(NewsArticle.id, NewsArticle.url).where(or_(*lookup_conditions))
        )
        url_to_id: dict[str, int] = {}
        for row in id_rows.all():
            article_id, url = row[0], row[1]
            try:
                key = canonicalize_naver_news_url(url)
            except ValueError:
                continue
            url_to_id.setdefault(key, article_id)
    else:
        urls = [item.url for item in items]
        id_rows = await db.execute(
            select(NewsArticle.id, NewsArticle.url).where(NewsArticle.url.in_(urls))
        )
        url_to_id = {url: article_id for article_id, url in id_rows.all()}

    link_values = []
    for item in link_items if symbol is not None else ():
        item_key = (
            canonicalize_naver_news_url(item.url)
            if canonical_dedupe and market == "kr"
            else item.url
        )
        article_id = url_to_id.get(item_key)
        if (
            article_id is None
        ):  # insert race lost and url missing — skip, next call heals
            continue
        link_values.append(
            {
                "article_id": article_id,
                "market": market,
                "symbol": symbol,
                "feed_source": feed_source,
                "first_seen_at": now,
                "status": "pending",
                "hints": build_relevance_hints(
                    symbol=symbol, market=market, title=item.title
                ),
                "created_at": now,
                "updated_at": now,
            }
        )
    new_links = 0
    if link_values:
        result = await db.execute(
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
        new_links = int(result.rowcount or 0)
    if commit:
        await db.commit()
    return new_articles, new_links


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> int:
    """KR 호환 래퍼 — 기존 호출부 보존용 (ROB-491)."""
    return await upsert_feed_articles(db, "kr", symbol, items, feed_source=feed_source)


async def upsert_kr_collector_articles(
    db: AsyncSession,
    symbol: str | None,
    items: list[FeedArticleInput],
    *,
    feed_source: str,
    commit: bool = True,
) -> tuple[int, int]:
    """Store collector articles and return (new articles, new symbol links)."""
    return await _upsert_feed_articles(
        db,
        "kr",
        symbol,
        items,
        feed_source=feed_source,
        canonical_dedupe=True,
        commit=commit,
    )


async def upsert_related_symbols(
    db: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    commit: bool = True,
) -> int:
    """Single write seam for `news_article_related_symbols` (ROB-916).

    ``rows`` are pre-built dicts matching the ORM column set (see
    ``app.services.news_payload_normalizer`` row builders). Idempotent by the
    ``(article_id, market, symbol, source)`` unique constraint — re-running
    over the same articles/matcher is always a no-op for existing rows.
    Callers that batch multiple writes in one transaction (e.g. the
    news-ingestor bulk endpoint) should pass ``commit=False`` and commit once
    at the end themselves.
    """
    if not rows:
        return 0
    result = await db.execute(
        pg_insert(NewsArticleRelatedSymbol)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[
                NewsArticleRelatedSymbol.article_id,
                NewsArticleRelatedSymbol.market,
                NewsArticleRelatedSymbol.symbol,
                NewsArticleRelatedSymbol.source,
            ]
        )
    )
    if commit:
        await db.commit()
    return int(result.rowcount or 0)


async def list_pending(
    db: AsyncSession,
    market: str,
    limit: int,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Pending links oldest-first with the article fields a judge needs."""
    conditions = [
        SymbolNewsRelevance.market == market,
        SymbolNewsRelevance.status == "pending",
    ]
    if symbol:
        conditions.append(SymbolNewsRelevance.symbol == symbol)
    stmt = (
        select(NewsArticle, SymbolNewsRelevance)
        .join(SymbolNewsRelevance, SymbolNewsRelevance.article_id == NewsArticle.id)
        .where(*conditions)
        .order_by(
            SymbolNewsRelevance.first_seen_at.asc(),
            SymbolNewsRelevance.id.asc(),
        )
        .limit(limit)
    )
    rows = await db.execute(stmt)
    return [
        {
            "article_id": article.id,
            "market": link.market,
            "symbol": link.symbol,
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "published_at": (
                article.article_published_at.isoformat()
                if article.article_published_at
                else None
            ),
            "first_seen_at": link.first_seen_at.isoformat(),
            "hints": link.hints,
        }
        for article, link in rows.all()
    ]


async def apply_judgment(
    db: AsyncSession,
    *,
    article_id: int,
    market: str,
    symbol: str,
    relationship: str,
    relevance: str,
    price_relevance: str,
    score: float | None,
    reason: str,
    judged_by: str,
) -> str | None:
    """Idempotent judgment write-back. Returns new status, None if link missing.

    Status is derived server-side (``derive_status``) — the job never sets it.
    """
    link = (
        await db.execute(
            select(SymbolNewsRelevance).where(
                SymbolNewsRelevance.article_id == article_id,
                SymbolNewsRelevance.market == market,
                SymbolNewsRelevance.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        return None
    now = _utcnow()
    link.relationship = relationship
    link.relevance = relevance
    link.price_relevance = price_relevance
    link.score = score
    link.reason = reason
    link.judged_by = judged_by
    link.judged_at = now
    link.updated_at = now
    link.status = derive_status(relationship, relevance)
    await db.flush()
    return link.status


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
            summary=article.summary,
            fetched_at=article.scraped_at,
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
