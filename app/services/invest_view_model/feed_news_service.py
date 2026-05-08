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
from app.services.news_entity_matcher import match_symbols_for_article
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


def _coerce_keywords(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [value]
    return []


def _add_related_symbol(
    *,
    related: list[NewsRelatedSymbol],
    seen_related: set[tuple[str, str]],
    resolver: RelationResolver,
    symbol: str | None,
    market: str,
    display_name: str | None,
    match_reason: str | None,
    matched_term: str | None = None,
) -> None:
    if not symbol or market not in ("kr", "us", "crypto"):
        return
    key = (market, symbol)
    if key in seen_related:
        return
    seen_related.add(key)
    related.append(
        NewsRelatedSymbol(
            symbol=symbol,
            market=cast(NewsMarket, market),
            displayName=display_name or symbol,
            relation=resolver.relation(market, symbol),
            matchReason=match_reason,
            matchedTerm=matched_term,
        )
    )


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
        market_typed = cast(NewsMarket, market_value)
        related: list[NewsRelatedSymbol] = []
        seen_related: set[tuple[str, str]] = set()

        if row.stock_symbol:
            _add_related_symbol(
                related=related,
                seen_related=seen_related,
                resolver=resolver,
                symbol=row.stock_symbol,
                market=market_value,
                display_name=row.stock_name or row.stock_symbol,
                match_reason="stock_symbol",
            )
        for match in match_symbols_for_article(
            title=row.title,
            summary=analysis_map.get(row.id) or row.summary,
            keywords=_coerce_keywords(getattr(row, "keywords", None)),
            market=market_value,
        ):
            _add_related_symbol(
                related=related,
                seen_related=seen_related,
                resolver=resolver,
                symbol=match.symbol,
                market=match.market,
                display_name=match.canonical_name,
                match_reason=match.reason,
                matched_term=match.matched_term,
            )
        related_relations = {symbol.relation for symbol in related}
        if "both" in related_relations or (
            "held" in related_relations and "watchlist" in related_relations
        ):
            relation = "both"
        elif "held" in related_relations:
            relation = "held"
        elif "watchlist" in related_relations:
            relation = "watchlist"
        else:
            relation = "none"
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                market=market_typed,
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
