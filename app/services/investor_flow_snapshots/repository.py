from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investor_flow_snapshot import InvestorFlowSnapshot


class InvestorFlowSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    snapshot_date: dt.date
    foreign_net: int | None = None
    institution_net: int | None = None
    individual_net: int | None = None
    foreign_net_buy_rank: int | None = None
    foreign_net_sell_rank: int | None = None
    institution_net_buy_rank: int | None = None
    institution_net_sell_rank: int | None = None
    double_buy: bool | None = None
    double_sell: bool | None = None
    # ROB-575 market fields (wired in ROB-640):
    close: Decimal | None = None
    # change_rate is stored as a percent (e.g. 1.5 for 1.5%)
    change_rate: Decimal | None = None
    volume: int | None = None
    foreign_holding_shares: int | None = None
    foreign_holding_rate: Decimal | None = None
    foreign_consecutive_buy_days: int | None = None
    foreign_consecutive_sell_days: int | None = None
    institution_consecutive_buy_days: int | None = None
    institution_consecutive_sell_days: int | None = None
    individual_consecutive_buy_days: int | None = None
    individual_consecutive_sell_days: int | None = None
    source: str
    collected_at: dt.datetime | None = None


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _with_derived_flags(payload: InvestorFlowSnapshotUpsert) -> dict:
    values = payload.model_dump(exclude_none=True)
    values["market"] = values["market"].strip().lower()
    values["symbol"] = _normalize_symbol(values["symbol"])
    foreign_net = values.get("foreign_net")
    institution_net = values.get("institution_net")
    if payload.double_buy is None:
        values["double_buy"] = bool(
            foreign_net is not None
            and institution_net is not None
            and foreign_net > 0
            and institution_net > 0
        )
    if payload.double_sell is None:
        values["double_sell"] = bool(
            foreign_net is not None
            and institution_net is not None
            and foreign_net < 0
            and institution_net < 0
        )
    if "collected_at" not in values:
        values["collected_at"] = dt.datetime.now(dt.UTC)
    return values


class InvestorFlowSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, payload: InvestorFlowSnapshotUpsert) -> None:
        values = _with_derived_flags(payload)
        stmt = insert(InvestorFlowSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_investor_flow_snapshots_market_symbol_date_source",
            set_={
                **{
                    key: stmt.excluded[key]
                    for key in values
                    if key not in {"market", "symbol", "snapshot_date", "source"}
                },
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def recent_by_symbol(
        self,
        *,
        market: str,
        symbol: str,
        as_of: dt.date | None = None,
        limit: int = 10,
    ) -> list[InvestorFlowSnapshot]:
        normalized_symbol = _normalize_symbol(symbol)
        if not normalized_symbol:
            return []
        stmt = select(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.market == market.strip().lower(),
            InvestorFlowSnapshot.symbol == normalized_symbol,
        )
        if as_of is not None:
            stmt = stmt.where(InvestorFlowSnapshot.snapshot_date <= as_of)
        result = await self._session.execute(
            stmt.order_by(
                InvestorFlowSnapshot.snapshot_date.desc(),
                InvestorFlowSnapshot.source.asc(),
            ).limit(limit)
        )
        rows = list(result.scalars().all())
        by_date: dict[dt.date, InvestorFlowSnapshot] = {}
        for row in rows:
            # Keep deterministic source precedence if multiple source snapshots exist.
            by_date.setdefault(row.snapshot_date, row)
        return list(by_date.values())

    async def latest_by_symbols(
        self,
        *,
        market: str,
        symbols: Iterable[str],
        as_of: dt.date | None = None,
    ) -> list[InvestorFlowSnapshot]:
        symbols_list = [
            _normalize_symbol(symbol) for symbol in symbols if symbol.strip()
        ]
        if not symbols_list:
            return []
        max_date_subq = select(
            InvestorFlowSnapshot.symbol.label("symbol"),
            func.max(InvestorFlowSnapshot.snapshot_date).label("snapshot_date"),
        ).where(
            InvestorFlowSnapshot.market == market.strip().lower(),
            InvestorFlowSnapshot.symbol.in_(symbols_list),
        )
        if as_of is not None:
            max_date_subq = max_date_subq.where(
                InvestorFlowSnapshot.snapshot_date <= as_of
            )
        max_date_subq = max_date_subq.group_by(InvestorFlowSnapshot.symbol).subquery()

        result = await self._session.execute(
            select(InvestorFlowSnapshot)
            .join(
                max_date_subq,
                (InvestorFlowSnapshot.symbol == max_date_subq.c.symbol)
                & (InvestorFlowSnapshot.snapshot_date == max_date_subq.c.snapshot_date),
            )
            .where(InvestorFlowSnapshot.market == market.strip().lower())
            .order_by(
                InvestorFlowSnapshot.symbol.asc(), InvestorFlowSnapshot.source.asc()
            )
        )
        rows = list(result.scalars().all())
        latest: dict[str, InvestorFlowSnapshot] = {}
        for row in rows:
            # Keep deterministic source precedence if multiple source snapshots exist.
            latest.setdefault(row.symbol, row)
        return [latest[symbol] for symbol in symbols_list if symbol in latest]
