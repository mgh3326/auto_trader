from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot


class SnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str
    symbol: str
    snapshot_date: dt.date
    latest_close: Decimal
    prev_close: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    consecutive_up_days: int | None = None
    week_change_rate: Decimal | None = None
    closes_window: list[Any] = Field(default_factory=list)
    daily_volume: int | None = None
    daily_turnover: Decimal | None = None
    market_cap: Decimal | None = None
    market_cap_source: str | None = None
    market_cap_snapshot_date: dt.date | None = None
    support_price: Decimal | None = None
    support_kind: str | None = None
    support_strength: str | None = None
    dist_to_support_pct: Decimal | None = None
    support_computed_at: dt.datetime | None = None
    source: str


@dataclass(frozen=True)
class CoverageCounts:
    market: str
    today_trading_date: dt.date
    fresh_count: int  # snapshot_date == today_trading_date
    stale_count: int  # snapshot_date < today_trading_date
    last_computed_at: dt.datetime | None


@dataclass(frozen=True)
class Breadth:
    market: str
    partition_date: dt.date | None
    total: int
    advancers: int
    decliners: int
    unchanged: int


class InvestScreenerSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, payload: SnapshotUpsert) -> None:
        values = payload.model_dump()
        stmt = insert(InvestScreenerSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_screener_snapshots_market_symbol_date",
            set_={
                **{
                    k: stmt.excluded[k]
                    for k in values
                    if k not in {"market", "symbol", "snapshot_date"}
                },
                "updated_at": func.now(),
                "computed_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def get_fresh(
        self,
        *,
        market: str,
        symbols: Iterable[str],
        on_or_after: dt.date,
    ) -> list[InvestScreenerSnapshot]:
        symbols_list = list(symbols)
        if not symbols_list:
            return []
        result = await self._session.execute(
            select(InvestScreenerSnapshot).where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.symbol.in_(symbols_list),
                InvestScreenerSnapshot.snapshot_date >= on_or_after,
            )
        )
        return list(result.scalars().all())

    async def coverage(
        self, *, market: str, today_trading_date: dt.date
    ) -> CoverageCounts:
        result = await self._session.execute(
            select(
                func.count()
                .filter(InvestScreenerSnapshot.snapshot_date == today_trading_date)
                .label("fresh"),
                func.count()
                .filter(InvestScreenerSnapshot.snapshot_date < today_trading_date)
                .label("stale"),
                func.max(InvestScreenerSnapshot.computed_at).label("last_computed_at"),
            ).where(InvestScreenerSnapshot.market == market)
        )
        row = result.one()
        return CoverageCounts(
            market=market,
            today_trading_date=today_trading_date,
            fresh_count=int(row.fresh or 0),
            stale_count=int(row.stale or 0),
            last_computed_at=row.last_computed_at,
        )

    async def latest_partition(self, *, market: str) -> dt.date | None:
        result = await self._session.execute(
            select(func.max(InvestScreenerSnapshot.snapshot_date)).where(
                InvestScreenerSnapshot.market == market
            )
        )
        return result.scalar_one_or_none()

    async def list_top_candidates(
        self, *, market: str, limit: int = 10
    ) -> list[InvestScreenerSnapshot]:
        latest = await self.latest_partition(market=market)
        if latest is None:
            return []
        result = await self._session.execute(
            select(InvestScreenerSnapshot)
            .where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
            .order_by(
                InvestScreenerSnapshot.change_rate.desc().nullslast(),
                InvestScreenerSnapshot.symbol.asc(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_candidate_pool(
        self, *, market: str, limit: int | None = None
    ) -> list[InvestScreenerSnapshot]:
        """ROB-346 — wide candidate pool from the latest partition. ``limit=None``
        returns the whole partition (no early cap); quality/priority filtering
        happens downstream in the collector."""
        latest = await self.latest_partition(market=market)
        if latest is None:
            return []
        stmt = (
            select(InvestScreenerSnapshot)
            .where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
            .order_by(
                InvestScreenerSnapshot.change_rate.desc().nullslast(),
                InvestScreenerSnapshot.symbol.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def common_stock_flags(self, symbols: list[str]) -> dict[str, bool | None]:
        """ROB-346 — us_symbol_universe.is_common_stock by symbol (US-only).
        Missing symbols are absent from the dict (caller treats as unknown)."""
        if not symbols:
            return {}
        from app.models.us_symbol_universe import USSymbolUniverse

        result = await self._session.execute(
            select(USSymbolUniverse.symbol, USSymbolUniverse.is_common_stock).where(
                USSymbolUniverse.symbol.in_(symbols)
            )
        )
        return dict(result.all())

    async def breadth(self, *, market: str) -> Breadth:
        latest = await self.latest_partition(market=market)
        if latest is None:
            return Breadth(
                market=market,
                partition_date=None,
                total=0,
                advancers=0,
                decliners=0,
                unchanged=0,
            )
        result = await self._session.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(InvestScreenerSnapshot.change_rate > 0)
                .label("adv"),
                func.count()
                .filter(InvestScreenerSnapshot.change_rate < 0)
                .label("dec"),
            ).where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.snapshot_date == latest,
            )
        )
        row = result.one()
        total, adv, dec = int(row.total or 0), int(row.adv or 0), int(row.dec or 0)
        return Breadth(
            market=market,
            partition_date=latest,
            total=total,
            advancers=adv,
            decliners=dec,
            unchanged=total - adv - dec,
        )
