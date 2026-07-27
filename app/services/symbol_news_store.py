"""Persistence seam for the symbol-news relevance lifecycle (ROB-491).

All DB writes for the get_news cache go through here: ① article/link upsert at
fetch time (set-difference by unique url — feed order is never trusted), and
② judgment apply via the token-authed ingest route (PR2). No MCP imports, no
LLM, no broker/order surface. Callers own session lifecycle and commit.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.symbol_news_relevance import build_relevance_hints

logger = logging.getLogger(__name__)

KR_FEED_SOURCE = "naver_item_news"
FINNHUB_COMPANY_FEED_SOURCE = "finnhub_company_news"  # us
FINNHUB_GENERAL_FEED_SOURCE = "finnhub_general_news"  # crypto (심볼 키 아님)

NEWS_RELEVANCE_CONTAMINATION_START = datetime(2026, 7, 26, 23, 0)
NEWS_RELEVANCE_CONTAMINATION_END = datetime(2026, 7, 27, 0, 30)
NEWS_RELEVANCE_CONTAMINATION_JUDGE = "hermes-news-relevance"
NEWS_RELEVANCE_CONTAMINATION_TITLE_PREFIX_LENGTH = 20


class NewsRelevanceRecoveryError(RuntimeError):
    """Base error for the bounded 2026-07-27 contamination repair."""


class NewsRelevanceRecoveryCountMismatch(NewsRelevanceRecoveryError):
    """Selected rows differ from the operator-approved expected count."""


class NewsRelevanceRecoverySnapshotRequired(NewsRelevanceRecoveryError):
    """Execution was requested without a previously exported audit snapshot."""


class NewsRelevanceRecoverySnapshotMismatch(NewsRelevanceRecoveryError):
    """Locked rows no longer match the pre-mutation audit snapshot."""


class NewsRelevanceRecoveryVerificationError(NewsRelevanceRecoveryError):
    """The flushed reset state did not match the selected row count."""


@dataclass(frozen=True)
class FeedArticleInput:
    url: str
    title: str
    source: str | None
    published_at: datetime | None
    summary: str | None = None


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


@dataclass(frozen=True)
class NewsRelevanceJudgmentSnapshot:
    """Recoverable pre-reset values for one contaminated judgment."""

    id: int
    article_id: int
    market: str
    symbol: str
    status: str
    relationship: str | None
    relevance: str | None
    price_relevance: str | None
    score: float | None
    reason: str | None
    judged_by: str | None
    judged_at: datetime | None


@dataclass(frozen=True)
class NewsRelevanceRecoveryResult:
    dry_run: bool
    selected: tuple[NewsRelevanceJudgmentSnapshot, ...]
    updated_count: int

    @property
    def selected_count(self) -> int:
        return len(self.selected)


def _utcnow() -> datetime:
    # Convention in this repo: naive UTC for DB storage to avoid asyncpg DataError
    return datetime.now(tz=UTC).replace(tzinfo=None)


def derive_status(relationship: str, relevance: str) -> str:
    """Server-owned status rule — the judgment job never writes status itself."""
    if relationship == "unrelated" or relevance == "low":
        return "excluded"
    return "confirmed"


def _contaminated_news_relevance_conditions() -> tuple[Any, ...]:
    """Exact bounded predicate proven against production read-only on 2026-07-27."""
    title_prefix = func.substr(
        NewsArticle.title,
        1,
        NEWS_RELEVANCE_CONTAMINATION_TITLE_PREFIX_LENGTH,
    )
    return (
        SymbolNewsRelevance.judged_at >= NEWS_RELEVANCE_CONTAMINATION_START,
        SymbolNewsRelevance.judged_at < NEWS_RELEVANCE_CONTAMINATION_END,
        SymbolNewsRelevance.judged_by == NEWS_RELEVANCE_CONTAMINATION_JUDGE,
        SymbolNewsRelevance.reason.is_not(None),
        NewsArticle.title != "",
        func.strpos(SymbolNewsRelevance.reason, title_prefix) > 0,
    )


def _judgment_snapshot(
    link: SymbolNewsRelevance,
) -> NewsRelevanceJudgmentSnapshot:
    return NewsRelevanceJudgmentSnapshot(
        id=link.id,
        article_id=link.article_id,
        market=link.market,
        symbol=link.symbol,
        status=link.status,
        relationship=link.relationship,
        relevance=link.relevance,
        price_relevance=link.price_relevance,
        score=link.score,
        reason=link.reason,
        judged_by=link.judged_by,
        judged_at=link.judged_at,
    )


async def recover_contaminated_news_relevance(
    db: AsyncSession,
    *,
    expected_count: int,
    execute: bool = False,
    audit_snapshot: Sequence[NewsRelevanceJudgmentSnapshot] | None = None,
) -> NewsRelevanceRecoveryResult:
    """Preview or reset only the attributable 2026-07-27 contaminated rows.

    Dry-run is the default and performs no mutation. Execution additionally
    requires an exact pre-reset snapshot (written to an audit artifact by the
    operator CLI), locks and rechecks those rows, then clears only judgment
    fields. The caller owns commit/rollback.
    """
    if expected_count <= 0:
        raise ValueError("expected_count must be positive")

    stmt = (
        select(SymbolNewsRelevance)
        .join(NewsArticle, NewsArticle.id == SymbolNewsRelevance.article_id)
        .where(*_contaminated_news_relevance_conditions())
        .order_by(
            SymbolNewsRelevance.judged_at.asc(),
            SymbolNewsRelevance.id.asc(),
        )
        .execution_options(populate_existing=True)
    )
    if execute:
        stmt = stmt.with_for_update(of=SymbolNewsRelevance)

    links = (await db.execute(stmt)).scalars().all()
    selected = tuple(_judgment_snapshot(link) for link in links)
    if len(selected) != expected_count:
        raise NewsRelevanceRecoveryCountMismatch(
            "news relevance recovery aborted: "
            f"expected {expected_count} contaminated rows, selected {len(selected)}"
        )

    if not execute:
        return NewsRelevanceRecoveryResult(
            dry_run=True,
            selected=selected,
            updated_count=0,
        )

    if audit_snapshot is None:
        raise NewsRelevanceRecoverySnapshotRequired(
            "news relevance recovery execution requires an exported audit snapshot"
        )
    if tuple(audit_snapshot) != selected:
        raise NewsRelevanceRecoverySnapshotMismatch(
            "news relevance recovery aborted: locked rows differ from audit snapshot"
        )

    now = _utcnow()
    for link in links:
        link.status = "pending"
        link.relationship = None
        link.relevance = None
        link.price_relevance = None
        link.score = None
        link.reason = None
        link.judged_by = None
        link.judged_at = None
        link.updated_at = now
    await db.flush()

    selected_ids = [row.id for row in selected]
    verified_count = (
        await db.execute(
            select(func.count())
            .select_from(SymbolNewsRelevance)
            .where(
                SymbolNewsRelevance.id.in_(selected_ids),
                SymbolNewsRelevance.status == "pending",
                SymbolNewsRelevance.relationship.is_(None),
                SymbolNewsRelevance.relevance.is_(None),
                SymbolNewsRelevance.price_relevance.is_(None),
                SymbolNewsRelevance.score.is_(None),
                SymbolNewsRelevance.reason.is_(None),
                SymbolNewsRelevance.judged_by.is_(None),
                SymbolNewsRelevance.judged_at.is_(None),
            )
        )
    ).scalar_one()
    if int(verified_count) != expected_count:
        raise NewsRelevanceRecoveryVerificationError(
            "news relevance recovery verification failed: "
            f"expected {expected_count} reset rows, verified {verified_count}"
        )

    return NewsRelevanceRecoveryResult(
        dry_run=False,
        selected=selected,
        updated_count=expected_count,
    )


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
    if not items:
        return 0
    now = _utcnow()
    article_values = [
        {
            "url": item.url,
            "title": item.title[:500],
            "source": item.source,
            "summary": item.summary,
            "market": market,
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
    await db.commit()
    return new_links


async def upsert_kr_feed_articles(
    db: AsyncSession,
    symbol: str,
    items: list[FeedArticleInput],
    *,
    feed_source: str = KR_FEED_SOURCE,
) -> int:
    """KR 호환 래퍼 — 기존 호출부 보존용 (ROB-491)."""
    return await upsert_feed_articles(db, "kr", symbol, items, feed_source=feed_source)


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
