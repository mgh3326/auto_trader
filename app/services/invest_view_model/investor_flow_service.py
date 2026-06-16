from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.schemas.investor_flow import InvestorFlowItem, InvestorFlowResponse
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _state_for_snapshot(
    row: InvestorFlowSnapshot,
    *,
    as_of: dt.date,
    max_stale_days: int,
) -> str:
    age_days = (as_of - row.snapshot_date).days
    if age_days <= max_stale_days:
        return "fresh"
    return "stale"


def _item_from_snapshot(
    row: InvestorFlowSnapshot,
    *,
    as_of: dt.date,
    max_stale_days: int,
) -> InvestorFlowItem:
    return InvestorFlowItem(
        symbol=row.symbol,
        market="kr",
        dataState=_state_for_snapshot(row, as_of=as_of, max_stale_days=max_stale_days),
        snapshotDate=row.snapshot_date,
        collectedAt=row.collected_at,
        source=row.source,
        foreignNet=row.foreign_net,
        institutionNet=row.institution_net,
        individualNet=row.individual_net,
        foreignHoldingShares=row.foreign_holding_shares,
        foreignHoldingRate=row.foreign_holding_rate,
        discussionSentimentRank=row.discussion_sentiment_rank,
        foreignNetBuyRank=row.foreign_net_buy_rank,
        foreignNetSellRank=row.foreign_net_sell_rank,
        institutionNetBuyRank=row.institution_net_buy_rank,
        institutionNetSellRank=row.institution_net_sell_rank,
        doubleBuy=row.double_buy,
        doubleSell=row.double_sell,
        foreignConsecutiveBuyDays=row.foreign_consecutive_buy_days,
        foreignConsecutiveSellDays=row.foreign_consecutive_sell_days,
        institutionConsecutiveBuyDays=row.institution_consecutive_buy_days,
        institutionConsecutiveSellDays=row.institution_consecutive_sell_days,
        individualConsecutiveBuyDays=row.individual_consecutive_buy_days,
        individualConsecutiveSellDays=row.individual_consecutive_sell_days,
    )


def _aggregate_state(items: list[InvestorFlowItem]) -> str:
    if not items:
        return "empty"
    states = {item.dataState for item in items}
    if states == {"fresh"}:
        return "fresh"
    if states == {"missing"}:
        return "missing"
    if states == {"stale"}:
        return "stale"
    return "partial"


async def latest_items_for_symbols(
    *,
    db: AsyncSession,
    symbols: list[str],
    market: str = "kr",
    as_of: dt.date | None = None,
    max_stale_days: int = 1,
) -> dict[str, InvestorFlowItem]:
    """Return {symbol -> fresh/stale InvestorFlowItem} for snapshots that exist.

    Symbols with no snapshot are absent from the dict. Read-only; no live fetch.
    """
    normalized_market = market.strip().lower()
    if normalized_market != "kr":
        raise ValueError("investor_flow only supports market=kr")
    today = as_of or dt.date.today()
    normalized_symbols = [
        _normalize_symbol(symbol) for symbol in symbols if symbol.strip()
    ]
    if not normalized_symbols:
        return {}
    repo = InvestorFlowSnapshotsRepository(db)
    rows = await repo.latest_by_symbols(
        market="kr", symbols=normalized_symbols, as_of=today
    )
    return {
        row.symbol: _item_from_snapshot(row, as_of=today, max_stale_days=max_stale_days)
        for row in rows
    }


async def build_investor_flow_cards(
    *,
    db: AsyncSession,
    symbols: list[str],
    market: str = "kr",
    as_of: dt.date | None = None,
    max_stale_days: int = 1,
) -> InvestorFlowResponse:
    normalized_market = market.strip().lower()
    if normalized_market != "kr":
        raise ValueError("investor_flow only supports market=kr")
    today = as_of or dt.date.today()
    normalized_symbols = [
        _normalize_symbol(symbol) for symbol in symbols if symbol.strip()
    ]
    if not normalized_symbols:
        return InvestorFlowResponse(
            market="kr", asOf=today, dataState="empty", items=[]
        )

    repo = InvestorFlowSnapshotsRepository(db)
    rows = await repo.latest_by_symbols(
        market="kr", symbols=normalized_symbols, as_of=today
    )
    rows_by_symbol = {row.symbol: row for row in rows}

    items: list[InvestorFlowItem] = []
    for symbol in normalized_symbols:
        row = rows_by_symbol.get(symbol)
        if row is None:
            items.append(
                InvestorFlowItem(symbol=symbol, market="kr", dataState="missing")
            )
            continue
        items.append(
            _item_from_snapshot(row, as_of=today, max_stale_days=max_stale_days)
        )

    sources = sorted({item.source for item in items if item.source})
    return InvestorFlowResponse(
        market="kr",
        asOf=today,
        source=sources[0] if len(sources) == 1 else None,
        dataState=_aggregate_state(items),
        items=items,
    )
