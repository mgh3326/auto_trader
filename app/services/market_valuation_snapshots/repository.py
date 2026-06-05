from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_valuation_snapshot import MarketValuationSnapshot


class MarketValuationSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    snapshot_date: dt.date
    source: str
    per: Decimal | None = None
    pbr: Decimal | None = None
    roe: Decimal | None = None
    dividend_yield: Decimal | None = None
    market_cap: Decimal | None = None
    high_52w: Decimal | None = None
    low_52w: Decimal | None = None
    high_52w_date: dt.date | None = None  # ROB-440 PR3: US 52w-high date (date-recency)
    raw_payload: dict | None = None


@dataclass(frozen=True)
class ValuationCoverageCounts:
    fresh_symbols: int
    stale_symbols: int
    latest_date: dt.date | None
    latest_at: dt.datetime | None
    total_symbols: int


def _normalize_payload(row: MarketValuationSnapshotUpsert) -> dict:
    values = row.model_dump()
    values["market"] = values["market"].strip().lower()
    values["symbol"] = values["symbol"].strip().upper()
    values["source"] = values["source"].strip().lower()
    return values


class MarketValuationSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[MarketValuationSnapshotUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        stmt = insert(MarketValuationSnapshot).values(payload)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_valuation_snapshots_market_symbol_date_source",
            set_={
                "per": stmt.excluded.per,
                "pbr": stmt.excluded.pbr,
                "roe": stmt.excluded.roe,
                "dividend_yield": stmt.excluded.dividend_yield,
                "market_cap": stmt.excluded.market_cap,
                "high_52w": stmt.excluded.high_52w,
                "low_52w": stmt.excluded.low_52w,
                "high_52w_date": stmt.excluded.high_52w_date,
                "raw_payload": stmt.excluded.raw_payload,
                "computed_at": func.now(),
                "updated_at": func.now(),
            },
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def coverage_counts(
        self, market: str, *, fresh_date: dt.date
    ) -> ValuationCoverageCounts:
        latest_subq = (
            select(
                MarketValuationSnapshot.symbol.label("symbol"),
                func.max(MarketValuationSnapshot.snapshot_date).label("latest_date"),
                func.max(MarketValuationSnapshot.computed_at).label("latest_at"),
            )
            .where(MarketValuationSnapshot.market == market.strip().lower())
            .group_by(MarketValuationSnapshot.symbol)
            .subquery()
        )
        row = (
            await self._session.execute(
                select(
                    func.count()
                    .filter(latest_subq.c.latest_date >= fresh_date)
                    .label("fresh"),
                    func.count()
                    .filter(latest_subq.c.latest_date < fresh_date)
                    .label("stale"),
                    func.max(latest_subq.c.latest_date).label("latest_date"),
                    func.max(latest_subq.c.latest_at).label("latest_at"),
                    func.count().label("total"),
                ).select_from(latest_subq)
            )
        ).one()
        return ValuationCoverageCounts(
            fresh_symbols=int(row.fresh or 0),
            stale_symbols=int(row.stale or 0),
            latest_date=row.latest_date,
            latest_at=row.latest_at,
            total_symbols=int(row.total or 0),
        )

    async def existing_keys(
        self, rows: Iterable[MarketValuationSnapshotUpsert]
    ) -> set[tuple[str, str, dt.date, str]]:
        keys = {
            (
                row.market.strip().lower(),
                row.symbol.strip().upper(),
                row.snapshot_date,
                row.source.strip().lower(),
            )
            for row in rows
        }
        if not keys:
            return set()
        conditions = [
            sa.and_(
                MarketValuationSnapshot.market == market,
                MarketValuationSnapshot.symbol == symbol,
                MarketValuationSnapshot.snapshot_date == snapshot_date,
                MarketValuationSnapshot.source == source,
            )
            for market, symbol, snapshot_date, source in keys
        ]
        result = await self._session.execute(
            select(
                MarketValuationSnapshot.market,
                MarketValuationSnapshot.symbol,
                MarketValuationSnapshot.snapshot_date,
                MarketValuationSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        return {(r.market, r.symbol, r.snapshot_date, r.source) for r in result.all()}

    async def latest_for_symbols(
        self, *, market: str, symbols: set[str]
    ) -> list[MarketValuationSnapshot]:
        if not symbols:
            return []
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols}
        stmt = (
            select(MarketValuationSnapshot)
            .where(
                MarketValuationSnapshot.market == norm_market,
                MarketValuationSnapshot.symbol.in_(norm_symbols),
            )
            .order_by(
                MarketValuationSnapshot.symbol.asc(),
                MarketValuationSnapshot.snapshot_date.desc(),
                MarketValuationSnapshot.computed_at.desc(),
            )
            .distinct(MarketValuationSnapshot.symbol)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
