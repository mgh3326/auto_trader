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

from app.models.analyst_consensus_snapshot import AnalystConsensusSnapshot


class AnalystConsensusSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    source: str
    snapshot_date: dt.date
    buy_count: int | None = None
    hold_count: int | None = None
    sell_count: int | None = None
    strong_buy_count: int | None = None
    total_count: int | None = None
    target_mean: Decimal | None = None
    target_median: Decimal | None = None
    target_high: Decimal | None = None
    target_low: Decimal | None = None
    upside_pct: Decimal | None = None
    analyst_count: int | None = None
    newest_opinion_date: dt.date | None = None
    current_price: Decimal | None = None
    raw_payload: dict | None = None


@dataclass(frozen=True)
class ConsensusCoverageCounts:
    fresh_symbols: int
    stale_symbols: int
    latest_snapshot_date: dt.date | None
    total_symbols: int


def _normalize_payload(row: AnalystConsensusSnapshotUpsert) -> dict:
    values = row.model_dump()
    values["market"] = values["market"].strip().lower()
    values["symbol"] = values["symbol"].strip().upper()
    values["source"] = values["source"].strip().lower()
    return values


class AnalystConsensusSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[AnalystConsensusSnapshotUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        stmt = insert(AnalystConsensusSnapshot).values(payload)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_analyst_consensus_snapshots_market_symbol_date_source",
            set_={
                "buy_count": stmt.excluded.buy_count,
                "hold_count": stmt.excluded.hold_count,
                "sell_count": stmt.excluded.sell_count,
                "strong_buy_count": stmt.excluded.strong_buy_count,
                "total_count": stmt.excluded.total_count,
                "target_mean": stmt.excluded.target_mean,
                "target_median": stmt.excluded.target_median,
                "target_high": stmt.excluded.target_high,
                "target_low": stmt.excluded.target_low,
                "upside_pct": stmt.excluded.upside_pct,
                "analyst_count": stmt.excluded.analyst_count,
                "newest_opinion_date": stmt.excluded.newest_opinion_date,
                "current_price": stmt.excluded.current_price,
                "raw_payload": stmt.excluded.raw_payload,
                "collected_at": func.now(),
            },
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def coverage_counts(
        self, market: str, *, fresh_after: dt.date
    ) -> ConsensusCoverageCounts:
        latest_subq = (
            select(
                AnalystConsensusSnapshot.symbol.label("symbol"),
                func.max(AnalystConsensusSnapshot.snapshot_date).label("latest_date"),
            )
            .where(AnalystConsensusSnapshot.market == market.strip().lower())
            .group_by(AnalystConsensusSnapshot.symbol)
            .subquery()
        )
        row = (
            await self._session.execute(
                select(
                    func.count()
                    .filter(latest_subq.c.latest_date >= fresh_after)
                    .label("fresh"),
                    func.count()
                    .filter(latest_subq.c.latest_date < fresh_after)
                    .label("stale"),
                    func.max(latest_subq.c.latest_date).label("latest_date"),
                    func.count().label("total"),
                ).select_from(latest_subq)
            )
        ).one()
        return ConsensusCoverageCounts(
            fresh_symbols=int(row.fresh or 0),
            stale_symbols=int(row.stale or 0),
            latest_snapshot_date=row.latest_date,
            total_symbols=int(row.total or 0),
        )

    async def existing_keys(
        self, rows: Iterable[AnalystConsensusSnapshotUpsert]
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
                AnalystConsensusSnapshot.market == market,
                AnalystConsensusSnapshot.symbol == symbol,
                AnalystConsensusSnapshot.snapshot_date == snapshot_date,
                AnalystConsensusSnapshot.source == source,
            )
            for market, symbol, snapshot_date, source in keys
        ]
        result = await self._session.execute(
            select(
                AnalystConsensusSnapshot.market,
                AnalystConsensusSnapshot.symbol,
                AnalystConsensusSnapshot.snapshot_date,
                AnalystConsensusSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        return {(r.market, r.symbol, r.snapshot_date, r.source) for r in result.all()}
