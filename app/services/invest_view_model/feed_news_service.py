"""ROB-142 — feed/news view-model assembler."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsAnalysisResult, NewsArticle
from app.schemas.invest_feed_news import (
    FeedNewsItem,
    FeedNewsMeta,
    FeedNewsResponse,
    FeedTab,
    NewsMarket,
    NewsRelatedSymbol,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.news_issue_clustering_service import build_market_issues


def _encode_cursor(published_at: datetime | None, article_id: int) -> str:
    payload = {
        "p": published_at.isoformat() if published_at else None,
        "i": article_id,
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, int | None]:
    if not cursor:
        return None, None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        p = payload.get("p")
        return (datetime.fromisoformat(p) if p else None, payload.get("i"))
    except Exception:
        return None, None


async def build_feed_news(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    tab: FeedTab,
    limit: int,
    cursor: str | None,
) -> FeedNewsResponse:
    market_filter: str | None = None
    if tab in ("kr", "us", "crypto"):
        market_filter = tab

    # Build market issues for the relevant window so each news item can be
    # linked to its clustered issue (ROB-148). For market-scoped tabs we
    # filter by that market; for other tabs we cluster across markets so
    # items from any market can be linked.
    issues_market = market_filter or "all"
    try:
        issues_resp = await build_market_issues(
            market=issues_market, window_hours=24, limit=20
        )
        issues = issues_resp.items
    except Exception:
        issues = []

    # Base news query.
    stmt = select(NewsArticle).order_by(
        desc(NewsArticle.article_published_at), desc(NewsArticle.id)
    )
    if market_filter:
        stmt = stmt.where(NewsArticle.market == market_filter)

    cursor_dt, cursor_id = _decode_cursor(cursor)
    if cursor_dt and cursor_id:
        stmt = stmt.where(
            (NewsArticle.article_published_at < cursor_dt)
            | (
                (NewsArticle.article_published_at == cursor_dt)
                & (NewsArticle.id < cursor_id)
            )
        )

    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last.article_published_at, last.id)
        rows = list(rows[:limit])

    # Bulk-load summaries for the page.
    article_ids = [r.id for r in rows]
    analysis_map: dict[int, str] = {}
    if article_ids:
        a_stmt = select(
            NewsAnalysisResult.article_id, NewsAnalysisResult.summary
        ).where(NewsAnalysisResult.article_id.in_(article_ids))
        for art_id, summary in (await db.execute(a_stmt)).all():
            analysis_map[art_id] = summary

    # ROB-148 — article_id → issue_id map for chip rendering.
    issue_id_for_article: dict[int, str] = {}
    for issue in issues:
        for article in issue.articles:
            # Keep the highest-ranked issue per article.
            issue_id_for_article.setdefault(article.id, issue.id)

    items: list[FeedNewsItem] = []
    for row in rows:
        market_value = (row.market or "kr").lower()
        if market_value not in ("kr", "us", "crypto"):
            continue
        related: list[NewsRelatedSymbol] = []
        if row.stock_symbol:
            related.append(
                NewsRelatedSymbol(
                    symbol=row.stock_symbol,
                    market=cast(NewsMarket, market_value),
                    displayName=row.stock_name or row.stock_symbol,
                )
            )
        relation = (
            resolver.relation(market_value, row.stock_symbol)
            if row.stock_symbol
            else "none"
        )
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                market=cast(NewsMarket, market_value),
                relatedSymbols=related,
                issueId=issue_id_for_article.get(row.id),
                summarySnippet=analysis_map.get(row.id) or row.summary,
                relation=relation,
                url=row.url,
            )
        )

    # Apply holdings/watchlist filters in-memory.
    empty_reason: str | None = None
    if tab == "holdings":
        before = len(items)
        items = [i for i in items if i.relation in ("held", "both")]
        if not resolver.held:
            empty_reason = "no_holdings"
        elif before > 0 and not items:
            empty_reason = "no_matching_news"
    elif tab == "watchlist":
        before = len(items)
        items = [i for i in items if i.relation in ("watchlist", "both")]
        if not resolver.watch:
            empty_reason = "no_watchlist"
        elif before > 0 and not items:
            empty_reason = "no_matching_news"

    return FeedNewsResponse(
        tab=tab,
        asOf=datetime.now(UTC),
        issues=issues,
        items=items,
        nextCursor=next_cursor,
        meta=FeedNewsMeta(emptyReason=empty_reason),
    )
