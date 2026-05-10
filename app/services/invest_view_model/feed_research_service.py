"""ROB-179 — /invest/api/feed/research view-model assembler."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_feed_research import (
    FeedResearchAppliedFilters,
    FeedResearchFilters,
    FeedResearchItem,
    FeedResearchMeta,
    FeedResearchResponse,
    FeedResearchTab,
)
from app.schemas.research_reports import ResearchReportSymbolCandidate
from app.services.invest_view_model.feed_cursor import (
    decode_feed_cursor,
    encode_feed_cursor,
)
from app.services.invest_view_model.relation_resolver import RelationResolver
from app.services.research_reports.query_service import ResearchReportsQueryService


def _derive_relation(
    symbol_candidates: list,
    resolver: RelationResolver,
) -> str:
    """Return mine|watch|none based on symbol_candidates overlap with resolver."""
    any_mine = False
    any_watch = False
    for sc in symbol_candidates:
        symbol = (
            sc.get("symbol") if isinstance(sc, dict) else getattr(sc, "symbol", None)
        )
        market = (
            sc.get("market") if isinstance(sc, dict) else getattr(sc, "market", None)
        )
        if not symbol or not market:
            continue
        rel = resolver.relation(market, symbol)
        if rel in ("held", "both"):
            any_mine = True
            break
        if rel == "watchlist":
            any_watch = True
    if any_mine:
        return "mine"
    if any_watch:
        return "watch"
    return "none"


def _derive_market(symbol_candidates: list) -> str | None:
    if not symbol_candidates:
        return None
    first = symbol_candidates[0]
    market = (
        first.get("market")
        if isinstance(first, dict)
        else getattr(first, "market", None)
    )
    if market in ("kr", "us", "crypto"):
        return market
    return None


def _row_to_item(row, resolver: RelationResolver) -> FeedResearchItem:
    candidates_raw = row.symbol_candidates or []
    candidates: list[ResearchReportSymbolCandidate] = []
    for sc in candidates_raw:
        try:
            candidates.append(ResearchReportSymbolCandidate.model_validate(sc))
        except Exception:
            continue

    excerpt = row.detail_excerpt or row.summary_text
    if excerpt and len(excerpt) > 500:
        excerpt = excerpt[:500]

    return FeedResearchItem(
        id=row.id,
        source=row.source,
        title=row.title or row.detail_title
        if hasattr(row, "detail_title")
        else row.title,
        analyst=row.analyst,
        publishedAtText=row.published_at_text,
        publishedAt=row.published_at,
        category=row.category,
        detailUrl=row.detail_url,
        pdfUrl=row.pdf_url,
        excerpt=excerpt,
        symbolCandidates=candidates,
        attributionPublisher=row.attribution_publisher,
        attributionCopyrightNotice=row.attribution_copyright_notice,
        market=_derive_market(candidates_raw),
        relation=_derive_relation(candidates_raw, resolver),
    )


def _empty_response(
    *,
    tab: FeedResearchTab,
    limit: int,
    filters: FeedResearchFilters,
) -> FeedResearchResponse:
    return FeedResearchResponse(
        tab=tab,
        asOf=datetime.now(UTC),
        items=[],
        nextCursor=None,
        meta=FeedResearchMeta(
            limit=limit,
            appliedFilters=FeedResearchAppliedFilters(
                source=filters.source,
                symbol=filters.symbol,
                analyst=filters.analyst,
                category=filters.category,
                query=filters.query,
                fromDate=filters.from_date,
                toDate=filters.to_date,
            ),
        ),
    )


async def build_feed_research(
    db: AsyncSession,
    resolver: RelationResolver,
    *,
    tab: FeedResearchTab,
    limit: int,
    cursor_str: str | None,
    filters: FeedResearchFilters,
) -> FeedResearchResponse:
    """Assemble /invest/api/feed/research response. Raises ValueError on bad cursor."""
    cursor: dict | None = None
    if cursor_str:
        cursor = decode_feed_cursor(cursor_str)

    market_filter: str | None = None
    symbol_in: list[str] | None = None

    if tab in ("mine", "holdings"):
        held_syms = {s for _, s in resolver.held}
        if not held_syms:
            # User has no holdings — return empty feed immediately
            return _empty_response(tab=tab, limit=limit, filters=filters)
        symbol_in = list(held_syms)
    elif tab == "watchlist":
        watch_syms = {s for _, s in resolver.watch}
        if not watch_syms:
            return _empty_response(tab=tab, limit=limit, filters=filters)
        symbol_in = list(watch_syms)
    elif tab == "top":
        combined = {s for _, s in resolver.held} | {s for _, s in resolver.watch}
        symbol_in = list(combined) if combined else None
    elif tab == "kr":
        market_filter = "kr"
    elif tab == "us":
        market_filter = "us"
    # tab == "latest" → no additional filter

    svc = ResearchReportsQueryService(db)
    rows, next_cursor_dict = await svc.find_feed_page(
        limit=limit,
        cursor=cursor,
        source=filters.source,
        symbol=filters.symbol,
        analyst=filters.analyst,
        category=filters.category,
        query=filters.query,
        from_date=filters.from_date,
        to_date=filters.to_date,
        market_filter=market_filter,
        symbol_in=symbol_in if symbol_in is not None else None,
    )

    items = [_row_to_item(row, resolver) for row in rows]

    next_cursor_str: str | None = None
    if next_cursor_dict is not None:
        next_cursor_str = encode_feed_cursor(
            datetime.fromisoformat(next_cursor_dict["p"])
            if next_cursor_dict["p"]
            else None,
            next_cursor_dict["i"],
        )

    return FeedResearchResponse(
        tab=tab,
        asOf=datetime.now(UTC),
        items=items,
        nextCursor=next_cursor_str,
        meta=FeedResearchMeta(
            limit=limit,
            appliedFilters=FeedResearchAppliedFilters(
                source=filters.source,
                symbol=filters.symbol,
                analyst=filters.analyst,
                category=filters.category,
                query=filters.query,
                fromDate=filters.from_date,
                toDate=filters.to_date,
            ),
        ),
    )
