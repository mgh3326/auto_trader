from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot


class KrFundamentalsSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    snapshot_date: dt.date
    name: str | None = None
    price: Decimal | None = None
    change_rate: Decimal | None = None
    volume: Decimal | None = None
    market_cap: Decimal | None = None
    per: Decimal | None = None
    pbr: Decimal | None = None
    dividend_yield: Decimal | None = None
    roe_ttm: Decimal | None = None
    payout_ratio_ttm: Decimal | None = None
    gross_margin_ttm: Decimal | None = None
    revenue_yoy: Decimal | None = None
    eps_yoy: Decimal | None = None
    eps_qoq: Decimal | None = None
    net_income_yoy: Decimal | None = None
    net_income_cagr_5y: Decimal | None = None
    continuous_dividend_payout: Decimal | None = None
    continuous_dividend_growth: Decimal | None = None
    week_high_52: Decimal | None = None
    week_high_52_date: dt.date | None = None
    rsi14: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "tvscreener_kr"


@dataclass(frozen=True)
class KrFundamentalsCoverageCounts:
    latest_partition_date: dt.date | None
    latest_partition_count: int
    stale_count: int
    last_computed_at: dt.datetime | None


class InvestKrFundamentalsSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, payload: KrFundamentalsSnapshotUpsert) -> None:
        values = payload.model_dump()
        stmt = insert(InvestKrFundamentalsSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_kr_fundamentals_snapshots_symbol_date",
            set_={
                **{
                    k: stmt.excluded[k]
                    for k in values
                    if k not in {"symbol", "snapshot_date"}
                },
                "updated_at": func.now(),
                "computed_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def latest_partition(self) -> dt.date | None:
        result = await self._session.execute(
            select(func.max(InvestKrFundamentalsSnapshot.snapshot_date))
        )
        return result.scalar_one_or_none()

    async def coverage(self, *, today: dt.date) -> KrFundamentalsCoverageCounts:
        latest = await self.latest_partition()
        if latest is None:
            return KrFundamentalsCoverageCounts(
                latest_partition_date=None,
                latest_partition_count=0,
                stale_count=0,
                last_computed_at=None,
            )
        result = await self._session.execute(
            sa.select(
                sa.func.count()
                .filter(InvestKrFundamentalsSnapshot.snapshot_date == latest)
                .label("latest_count"),
                sa.func.count()
                .filter(InvestKrFundamentalsSnapshot.snapshot_date < today)
                .label("stale"),
                sa.func.max(InvestKrFundamentalsSnapshot.computed_at).label("last"),
            )
        )
        row = result.one()
        return KrFundamentalsCoverageCounts(
            latest_partition_date=latest,
            latest_partition_count=int(row.latest_count or 0),
            stale_count=int(row.stale or 0),
            last_computed_at=row.last,
        )
