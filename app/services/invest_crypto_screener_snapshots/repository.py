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

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot


class CryptoSnapshotUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    snapshot_date: dt.date
    name: str | None = None
    latest_close: Decimal
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    trade_amount_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    market_cap: Decimal | None = None
    rsi: Decimal | None = None
    adx: Decimal | None = None
    funding_rate: Decimal | None = None  # ROB-443: Binance perp; None when no perp
    market_warning: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "tvscreener_upbit"


@dataclass(frozen=True)
class CryptoCoverageCounts:
    latest_partition_date: dt.date | None
    latest_partition_count: int
    stale_count: int
    last_computed_at: dt.datetime | None


class InvestCryptoScreenerSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, payload: CryptoSnapshotUpsert) -> None:
        values = payload.model_dump()
        stmt = insert(InvestCryptoScreenerSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_crypto_screener_snapshots_symbol_date",
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
            select(func.max(InvestCryptoScreenerSnapshot.snapshot_date))
        )
        return result.scalar_one_or_none()

    async def list_latest(
        self,
        *,
        preset_id: str,
        limit: int = 20,
        snapshot_date: dt.date | None = None,
    ) -> list[InvestCryptoScreenerSnapshot]:
        latest_date = snapshot_date or await self.latest_partition()
        if latest_date is None:
            return []

        stmt = select(InvestCryptoScreenerSnapshot).where(
            InvestCryptoScreenerSnapshot.snapshot_date == latest_date,
            InvestCryptoScreenerSnapshot.market_warning.is_(False),
        )
        if preset_id == "crypto_oversold":
            stmt = stmt.where(
                InvestCryptoScreenerSnapshot.rsi.is_not(None),
                InvestCryptoScreenerSnapshot.rsi <= 35,
            ).order_by(
                InvestCryptoScreenerSnapshot.rsi.asc().nullslast(),
                InvestCryptoScreenerSnapshot.trade_amount_24h.desc().nullslast(),
                InvestCryptoScreenerSnapshot.symbol.asc(),
            )
        elif preset_id == "crypto_momentum":
            stmt = stmt.order_by(
                InvestCryptoScreenerSnapshot.change_rate.desc().nullslast(),
                InvestCryptoScreenerSnapshot.trade_amount_24h.desc().nullslast(),
                InvestCryptoScreenerSnapshot.symbol.asc(),
            )
        else:
            stmt = stmt.order_by(
                InvestCryptoScreenerSnapshot.trade_amount_24h.desc().nullslast(),
                InvestCryptoScreenerSnapshot.symbol.asc(),
            )
        result = await self._session.execute(stmt.limit(limit))
        return list(result.scalars().all())

    async def coverage(self, *, today: dt.date) -> CryptoCoverageCounts:
        latest = await self.latest_partition()
        if latest is None:
            return CryptoCoverageCounts(
                latest_partition_date=None,
                latest_partition_count=0,
                stale_count=0,
                last_computed_at=None,
            )
        result = await self._session.execute(
            sa.select(
                sa.func.count()
                .filter(InvestCryptoScreenerSnapshot.snapshot_date == latest)
                .label("latest_count"),
                sa.func.count()
                .filter(InvestCryptoScreenerSnapshot.snapshot_date < today)
                .label("stale"),
                sa.func.max(InvestCryptoScreenerSnapshot.computed_at).label("last"),
            )
        )
        row = result.one()
        return CryptoCoverageCounts(
            latest_partition_date=latest,
            latest_partition_count=int(row.latest_count or 0),
            stale_count=int(row.stale or 0),
            last_computed_at=row.last,
        )
