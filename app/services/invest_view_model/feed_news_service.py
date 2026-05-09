"""ROB-142 — feed/news view-model assembler."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsAnalysisResult, NewsArticle, NewsArticleRelatedSymbol
from app.schemas.invest_feed_news import (
    FeedNewsItem,
    FeedNewsMeta,
    FeedNewsResponse,
    FeedTab,
    NewsMarket,
    NewsRelatedSymbol,
    NewsScope,
)
from app.services.crypto_news_relevance_service import (
    score_crypto_news_article,
    user_facing_category,
)
from app.services.domain_errors import (
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.market_data.service import get_quote
from app.services.news_entity_matcher import (
    classify_article_scope,
    match_symbols_for_article,
)
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


_QUOTE_FAILURE_EXCEPTIONS = (
    ValidationError,
    SymbolNotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
)


def _market_for_quote(market: str) -> str:
    if market == "kr":
        return "kr"
    if market == "us":
        return "us"
    return "crypto"


def _relation_from_related_symbols(related: list[NewsRelatedSymbol]) -> str:
    relations = {symbol.relation for symbol in related}
    if "both" in relations or {"held", "watchlist"}.issubset(relations):
        return "both"
    if "held" in relations:
        return "held"
    if "watchlist" in relations:
        return "watchlist"
    return "none"


def _related_symbols_for_article(
    *,
    row: NewsArticle,
    resolver: RelationResolver,
    analysis_summary: str | None,
    persisted_relations: dict[int, list[NewsArticleRelatedSymbol]],
    market_value: str,
) -> tuple[list[NewsRelatedSymbol], NewsScope, list[str], list[str]]:
    """Return (related_symbols, scope, scope_tags, demoted_symbol_keys).

    scope, scope_tags, and demoted_symbol_keys are derived from the US scope
    classifier (ROB-155) and default to symbol_specific/empty for non-US
    articles. demoted_symbol_keys is a list of (market, symbol) key strings used
    by the caller to filter relatedSymbols.
    """
    related: list[NewsRelatedSymbol] = []
    seen_related: set[tuple[str, str]] = set()

    for persisted in persisted_relations.get(row.id, []):
        _add_related_symbol(
            related=related,
            seen_related=seen_related,
            resolver=resolver,
            symbol=persisted.symbol,
            market=persisted.market,
            display_name=persisted.display_name or persisted.symbol,
            match_reason=persisted.source,
            matched_term=persisted.matched_term,
        )

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

    alias_matches = match_symbols_for_article(
        title=row.title,
        summary=analysis_summary or row.summary,
        keywords=_coerce_keywords(getattr(row, "keywords", None)),
        market=market_value,
    )
    for match in alias_matches:
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

    # ROB-155: US scope classification — demote incidental big-tech symbols in response.
    scope: NewsScope = "symbol_specific"
    scope_tags: list[str] = []
    demoted_symbol_keys: list[str] = []
    if market_value == "us":
        scope_result = classify_article_scope(
            row.title,
            summary=analysis_summary or row.summary,
            keywords=_coerce_keywords(getattr(row, "keywords", None)),
            market=market_value,
            matches=alias_matches,
        )
        scope = cast(NewsScope, scope_result.scope)
        scope_tags = list(scope_result.tags)
        demoted_symbol_keys = [f"us:{s}" for s in scope_result.demoted_symbols]

    return related, scope, scope_tags, demoted_symbol_keys


async def _related_symbols_by_article(
    db: AsyncSession, article_ids: list[int]
) -> dict[int, list[NewsArticleRelatedSymbol]]:
    if not article_ids:
        return {}
    stmt = (
        select(NewsArticleRelatedSymbol)
        .where(NewsArticleRelatedSymbol.article_id.in_(article_ids))
        .order_by(
            NewsArticleRelatedSymbol.article_id,
            NewsArticleRelatedSymbol.rank.asc().nulls_last(),
            NewsArticleRelatedSymbol.id,
        )
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_article: dict[int, list[NewsArticleRelatedSymbol]] = {}
    for row in rows:
        by_article.setdefault(row.article_id, []).append(row)
    return by_article


def _collect_related_symbol_pairs(
    items: list[FeedNewsItem],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        for symbol in item.relatedSymbols:
            key = (symbol.market, symbol.symbol)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    return pairs


async def _fetch_quotes_for_pairs(
    pairs: list[tuple[str, str]],
) -> tuple[dict[tuple[str, str], object], list[str], int]:
    quote_by_pair: dict[tuple[str, str], object] = {}
    warnings: list[str] = []
    failures = 0
    for market, symbol in pairs:
        try:
            quote_by_pair[(market, symbol)] = await get_quote(
                symbol=symbol, market=_market_for_quote(market)
            )
        except _QUOTE_FAILURE_EXCEPTIONS:
            failures += 1
            warnings.append(f"quote_unavailable:{market}:{symbol}")
        except Exception:
            failures += 1
            warnings.append(f"quote_unavailable:{market}:{symbol}")
    return quote_by_pair, warnings, failures


def _apply_quote_to_related_symbol(
    symbol: NewsRelatedSymbol,
    quote: object,
    as_of: datetime,
) -> None:
    price = getattr(quote, "price", None)
    previous_close = getattr(quote, "previous_close", None)
    symbol.currentPrice = float(price) if price is not None else None
    symbol.previousClose = float(previous_close) if previous_close is not None else None
    if symbol.currentPrice is not None and symbol.previousClose:
        symbol.change = symbol.currentPrice - symbol.previousClose
        symbol.changePct = (symbol.change / symbol.previousClose) * 100
    symbol.quoteSource = getattr(quote, "source", None)
    symbol.quoteAsOf = as_of


def _apply_quotes_to_items(
    items: list[FeedNewsItem],
    quote_by_pair: dict[tuple[str, str], object],
) -> None:
    as_of = datetime.now(UTC)
    for item in items:
        for symbol in item.relatedSymbols:
            quote = quote_by_pair.get((symbol.market, symbol.symbol))
            if quote is None:
                continue
            _apply_quote_to_related_symbol(symbol, quote, as_of)


async def _enrich_related_symbols_with_quotes(
    items: list[FeedNewsItem],
) -> list[str]:
    pairs = _collect_related_symbol_pairs(items)
    quote_by_pair, warnings, failures = await _fetch_quotes_for_pairs(pairs)
    _apply_quotes_to_items(items, quote_by_pair)
    if failures:
        warnings.append(f"quote_partial_failure:{failures}")
    return warnings


async def build_feed_news(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    tab: FeedTab,
    limit: int,
    cursor: str | None,
    include_quotes: bool = False,
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
        NewsArticle.article_published_at.desc().nulls_last(), desc(NewsArticle.id)
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

    # Bulk-load summaries and persisted related-symbol rows for the page.
    article_ids = [r.id for r in rows]
    analysis_map: dict[int, str] = {}
    if article_ids:
        a_stmt = select(
            NewsAnalysisResult.article_id, NewsAnalysisResult.summary
        ).where(NewsAnalysisResult.article_id.in_(article_ids))
        for art_id, summary in (await db.execute(a_stmt)).all():
            analysis_map[art_id] = summary
    persisted_relations = await _related_symbols_by_article(db, article_ids)

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
        analysis_summary = analysis_map.get(row.id)
        related, item_scope, scope_tags, demoted_keys = _related_symbols_for_article(
            row=row,
            resolver=resolver,
            analysis_summary=analysis_summary,
            persisted_relations=persisted_relations,
            market_value=market_value,
        )

        # ROB-155: remove demoted big-tech symbols from response for market_wide US articles.
        if demoted_keys:
            related = [
                s for s in related if f"{s.market}:{s.symbol}" not in demoted_keys
            ]

        # ROB-155: apply crypto relevance scoring for crypto articles.
        item_category: str | None = None
        item_noise_reason: str | None = None
        if market_value == "crypto":
            relevance = score_crypto_news_article(row)
            item_category = user_facing_category(relevance.category)
            item_noise_reason = relevance.noise_reason
            # Demote relatedSymbols for low-relevance crypto articles.
            if not relevance.include_in_briefing:
                item_category = item_category or "low_relevance"
                if "crypto_low_relevance" not in scope_tags:
                    scope_tags.append("crypto_low_relevance")
                if related:
                    related = []

        relation = _relation_from_related_symbols(related)
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
                summarySnippet=analysis_summary or row.summary,
                relation=relation,
                url=row.url,
                scope=cast(NewsScope, item_scope),
                tags=scope_tags,
                category=item_category,
                noiseReason=item_noise_reason,
            )
        )

    # ROB-155: filter out very low relevance crypto rows only on crypto tab to avoid
    # polluting the feed. Keep all rows for other tabs (pagination remains intact for
    # non-crypto tabs). Conservative: only drop rows with noise AND no relatedSymbols.
    if tab == "crypto":
        items = [i for i in items if not (i.noiseReason and not i.relatedSymbols)]

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

    warnings: list[str] = []
    if include_quotes:
        warnings.extend(await _enrich_related_symbols_with_quotes(items))

    return FeedNewsResponse(
        tab=tab,
        asOf=datetime.now(UTC),
        issues=issues,
        items=items,
        nextCursor=next_cursor,
        meta=FeedNewsMeta(emptyReason=empty_reason, warnings=warnings),
    )
