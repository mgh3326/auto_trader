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

# asyncpg caps bound arguments per statement at 32767 (signed int16 wire
# field). Toss symbol-master commits can produce thousands of market_cap rows;
# with 13 inserted columns, one KR/US all-symbol upsert can exceed the ceiling.
# Keep a margin for future column growth and derive chunk rows from the actual
# payload width.
_MAX_BIND_PARAMS = 30_000


def _chunk_rows_for_columns(column_count: int) -> int:
    """Rows per upsert statement that keep bind params under the asyncpg ceiling."""
    return max(1, _MAX_BIND_PARAMS // max(1, column_count))


def metric_rich_filter() -> sa.ColumnElement[bool]:
    """ROB-551: a valuation row is "metric-rich" when it carries at least one
    fundamentals metric (per/pbr/roe/dividend_yield). A market_cap-only row
    (e.g. ``source='toss_openapi'`` gap-fill) is metric-sparse.

    Use this as ``row_filter`` for ``resolve_healthy_partition`` on
    MarketValuationSnapshot so a toss-only partition (0 metric-rich rows) is not
    selected as the screener val_date and then emptied by the per>0/pbr>0
    candidate filters downstream.
    """
    return sa.or_(
        MarketValuationSnapshot.per.isnot(None),
        MarketValuationSnapshot.pbr.isnot(None),
        MarketValuationSnapshot.roe.isnot(None),
        MarketValuationSnapshot.dividend_yield.isnot(None),
    )


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
        chunk_rows = _chunk_rows_for_columns(len(payload[0]))
        total = 0
        for start in range(0, len(payload), chunk_rows):
            total += await self._upsert_chunk(payload[start : start + chunk_rows])
        return total

    async def _upsert_chunk(self, chunk: list[dict]) -> int:
        stmt = insert(MarketValuationSnapshot).values(chunk)
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

    async def symbols_with_other_source(
        self,
        *,
        market: str,
        snapshot_date: dt.date,
        symbols: set[str],
        exclude_source: str,
    ) -> set[str]:
        """ROB-546 gap-fill: symbols that already have a valuation row from a
        source other than ``exclude_source`` for ``(market, snapshot_date)``.

        Used to skip writing metric-sparse Toss market_cap rows where a
        metric-rich source (naver_finance/yahoo) already covers the key.
        """
        if not symbols:
            return set()
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols}
        norm_exclude = exclude_source.strip().lower()
        stmt = (
            select(MarketValuationSnapshot.symbol)
            .where(
                MarketValuationSnapshot.market == norm_market,
                MarketValuationSnapshot.snapshot_date == snapshot_date,
                MarketValuationSnapshot.symbol.in_(norm_symbols),
                MarketValuationSnapshot.source != norm_exclude,
            )
            .distinct()
        )
        result = await self._session.execute(stmt)
        return {row[0] for row in result.all()}

    async def latest_for_symbols(
        self, *, market: str, symbols: set[str]
    ) -> list[MarketValuationSnapshot]:
        if not symbols:
            return []
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols}
        # ROB-546: prefer rows that actually carry fundamentals metrics so a
        # metric-sparse market_cap-only row (e.g. toss_openapi, per/pbr/roe NULL)
        # on a newer snapshot_date does not shadow a metric-rich row. Falls back
        # to the sparse row only when it is the symbol's sole source.
        metric_sparse = sa.case((metric_rich_filter(), 0), else_=1)
        stmt = (
            select(MarketValuationSnapshot)
            .where(
                MarketValuationSnapshot.market == norm_market,
                MarketValuationSnapshot.symbol.in_(norm_symbols),
            )
            .order_by(
                MarketValuationSnapshot.symbol.asc(),
                metric_sparse.asc(),
                MarketValuationSnapshot.snapshot_date.desc(),
                MarketValuationSnapshot.computed_at.desc(),
            )
            .distinct(MarketValuationSnapshot.symbol)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
