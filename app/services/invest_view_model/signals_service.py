"""ROB-143 — signals view-model assembler."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockAnalysisResult, StockInfo
from app.schemas.invest_signals import (
    DecisionLabel,
    SignalCard,
    SignalMarket,
    SignalRelatedSymbol,
    SignalsMeta,
    SignalSource,
    SignalsResponse,
    SignalTab,
)
from app.services.invest_view_model.relation_resolver import RelationResolver


def _market_from_instrument_type(t: str | None) -> SignalMarket | None:
    if not t:
        return None
    t = t.lower()
    if t in ("crypto", "coin"):
        return "crypto"
    if "us" in t or t == "overseas":
        return "us"
    if "kr" in t or t == "domestic":
        return "kr"
    return None


def _decision_label(value: str | None) -> DecisionLabel | None:
    if not value:
        return None
    v = value.lower()
    if v in ("buy", "hold", "sell", "watch", "neutral"):
        return cast(DecisionLabel, v)
    return None


async def build_signals(
    *,
    db: AsyncSession,
    resolver: RelationResolver,
    tab: SignalTab,
    limit: int,
) -> SignalsResponse:
    # Latest analysis per stock_info_id (window function would be ideal; use simple top-N for MVP).
    stmt = (
        select(StockAnalysisResult, StockInfo)
        .join(StockInfo, StockInfo.id == StockAnalysisResult.stock_info_id)
        .order_by(desc(StockAnalysisResult.created_at))
        .limit(limit * 4)  # over-fetch then dedupe
    )
    seen_symbols: set[tuple[str, str]] = set()
    cards: list[SignalCard] = []
    for analysis, info in (await db.execute(stmt)).all():
        market = _market_from_instrument_type(info.instrument_type) or "kr"
        key = (market, info.symbol or "")
        if key in seen_symbols:
            continue
        seen_symbols.add(key)
        relation = resolver.relation(market, info.symbol or "")
        if tab == "mine" and relation == "none":
            continue
        if tab in ("kr", "us", "crypto") and tab != market:
            continue
        cards.append(
            SignalCard(
                id=f"analysis:{analysis.id}",
                source=cast(SignalSource, "analysis"),
                title=info.name or info.symbol or "(unknown)",
                market=cast(SignalMarket, market),
                decisionLabel=_decision_label(getattr(analysis, "decision", None)),
                confidence=(
                    int(analysis.confidence)
                    if getattr(analysis, "confidence", None) is not None
                    else None
                ),
                severity=None,
                summary=getattr(analysis, "detailed_text", None),
                generatedAt=analysis.created_at,
                relatedSymbols=(
                    [
                        SignalRelatedSymbol(
                            symbol=info.symbol or "",
                            market=cast(SignalMarket, market),
                            displayName=info.name or info.symbol or "",
                        )
                    ]
                    if info.symbol
                    else []
                ),
                relation=relation,
                rationale=(
                    str(getattr(analysis, "reasons", None))
                    if getattr(analysis, "reasons", None)
                    else None
                ),
            )
        )
        if len(cards) >= limit:
            break

    empty_reason: str | None = None
    if tab == "mine" and not cards:
        if not resolver.held and not resolver.watch:
            empty_reason = "no_holdings_or_watchlist"
        else:
            empty_reason = "no_matching_signals"

    return SignalsResponse(
        tab=tab,
        asOf=datetime.now(UTC),
        items=cards,
        meta=SignalsMeta(emptyReason=empty_reason),
    )
