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

from app.models.market_quote_snapshot import MarketQuoteSnapshot


class MarketQuoteSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    source: str
    snapshot_at: dt.datetime
    price: Decimal
    previous_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None
    raw_payload: dict | None = None


@dataclass(frozen=True)
class QuoteCoverageCounts:
    fresh_symbols: int
    stale_symbols: int
    latest_snapshot_at: dt.datetime | None
    total_symbols: int


def _normalize_payload(row: MarketQuoteSnapshotUpsert) -> dict:
    values = row.model_dump()
    values["market"] = values["market"].strip().lower()
    values["symbol"] = values["symbol"].strip().upper()
    values["source"] = values["source"].strip().lower()
    values["snapshot_at"] = values["snapshot_at"].replace(microsecond=0)
    return values


class MarketQuoteSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[MarketQuoteSnapshotUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        stmt = insert(MarketQuoteSnapshot).values(payload)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_market_quote_snapshots_market_symbol_source_at",
            set_={
                "price": stmt.excluded.price,
                "previous_close": stmt.excluded.previous_close,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "volume": stmt.excluded.volume,
                "raw_payload": stmt.excluded.raw_payload,
                "collected_at": func.now(),
            },
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def coverage_counts(
        self, market: str, *, fresh_after: dt.datetime
    ) -> QuoteCoverageCounts:
        latest_subq = (
            select(
                MarketQuoteSnapshot.symbol.label("symbol"),
                func.max(MarketQuoteSnapshot.snapshot_at).label("latest_at"),
            )
            .where(MarketQuoteSnapshot.market == market.strip().lower())
            .group_by(MarketQuoteSnapshot.symbol)
            .subquery()
        )
        row = (
            await self._session.execute(
                select(
                    func.count()
                    .filter(latest_subq.c.latest_at >= fresh_after)
                    .label("fresh"),
                    func.count()
                    .filter(latest_subq.c.latest_at < fresh_after)
                    .label("stale"),
                    func.max(latest_subq.c.latest_at).label("latest_at"),
                    func.count().label("total"),
                ).select_from(latest_subq)
            )
        ).one()
        return QuoteCoverageCounts(
            fresh_symbols=int(row.fresh or 0),
            stale_symbols=int(row.stale or 0),
            latest_snapshot_at=row.latest_at,
            total_symbols=int(row.total or 0),
        )

    async def existing_keys(
        self, rows: Iterable[MarketQuoteSnapshotUpsert]
    ) -> set[tuple[str, str, str, dt.datetime]]:
        keys = {
            (
                row.market.strip().lower(),
                row.symbol.strip().upper(),
                row.source.strip().lower(),
                row.snapshot_at.replace(microsecond=0),
            )
            for row in rows
        }
        if not keys:
            return set()
        conditions = [
            sa.and_(
                MarketQuoteSnapshot.market == market,
                MarketQuoteSnapshot.symbol == symbol,
                MarketQuoteSnapshot.source == source,
                MarketQuoteSnapshot.snapshot_at == snapshot_at,
            )
            for market, symbol, source, snapshot_at in keys
        ]
        result = await self._session.execute(
            select(
                MarketQuoteSnapshot.market,
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.source,
                MarketQuoteSnapshot.snapshot_at,
            ).where(sa.or_(*conditions))
        )
        return {(r.market, r.symbol, r.source, r.snapshot_at) for r in result.all()}

    async def latest_prices(self, market: str, symbols: list[str]) -> dict[str, float]:
        """ROB-696 — latest close per symbol (any source) for the KIS→Toss→
        snapshot fallback's last hop. Read-only; missing symbols are absent."""
        if not symbols:
            return {}
        upper = [s.strip().upper() for s in symbols if s.strip()]
        if not upper:
            return {}
        stmt = (
            select(MarketQuoteSnapshot.symbol, MarketQuoteSnapshot.price)
            .where(
                MarketQuoteSnapshot.market == market.strip().lower(),
                MarketQuoteSnapshot.symbol.in_(upper),
            )
            .distinct(MarketQuoteSnapshot.symbol)
            .order_by(
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.snapshot_at.desc(),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return {row.symbol: float(row.price) for row in rows}
