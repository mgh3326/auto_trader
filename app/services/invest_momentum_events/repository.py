from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_momentum_event_snapshot import (
    InvestMomentumEventSnapshot,
    InvestThemeEventSnapshot,
    InvestThemeEventSnapshotStock,
)
from app.services.invest_momentum_events.models import (
    MomentumEventUpsert,
    ThemeEventStockUpsert,
    ThemeEventUpsert,
)


@dataclass(frozen=True)
class SnapshotCoverage:
    market: str
    as_of: dt.date
    momentum_count: int
    theme_count: int
    last_momentum_snapshot_at: dt.datetime | None
    last_theme_snapshot_at: dt.datetime | None


class InvestMomentumEventSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_momentum(self, payload: MomentumEventUpsert) -> None:
        values = payload.model_dump()
        stmt = insert(InvestMomentumEventSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_momentum_event_snapshots_surface_params_symbol_at",
            set_={
                **{
                    k: stmt.excluded[k]
                    for k in values
                    if k
                    not in {
                        "surface",
                        "snapshot_at",
                        "trade_type",
                        "market_type",
                        "order_type",
                        "symbol",
                    }
                },
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def upsert_theme(self, payload: ThemeEventUpsert) -> int | None:
        values = payload.model_dump(exclude={"stocks"})
        stmt = (
            insert(InvestThemeEventSnapshot)
            .values(**values)
            .returning(InvestThemeEventSnapshot.id)
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_invest_theme_event_snapshots_at_key",
            set_={
                **{
                    k: stmt.excluded[k]
                    for k in values
                    if k not in {"snapshot_at", "source_event_key"}
                },
                "updated_at": func.now(),
            },
        ).returning(InvestThemeEventSnapshot.id)
        result = await self._session.execute(stmt)
        theme_id = result.scalar_one_or_none()
        if theme_id is not None and payload.stocks:
            await self.replace_theme_stocks(theme_id, payload.stocks)
        return theme_id

    async def replace_theme_stocks(
        self, theme_snapshot_id: int, payloads: list[ThemeEventStockUpsert]
    ) -> None:
        await self._session.execute(
            delete(InvestThemeEventSnapshotStock).where(
                InvestThemeEventSnapshotStock.theme_snapshot_id == theme_snapshot_id
            )
        )
        for payload in payloads:
            await self._session.execute(
                insert(InvestThemeEventSnapshotStock).values(
                    theme_snapshot_id=theme_snapshot_id, **payload.model_dump()
                )
            )

    async def list_momentum_events(
        self,
        *,
        trading_date: dt.date | None = None,
        surface: str | None = None,
        order_type: str | None = None,
        trade_type: str | None = None,
        limit: int = 50,
    ) -> list[InvestMomentumEventSnapshot]:
        conditions = [InvestMomentumEventSnapshot.market == "kr"]
        if trading_date is not None:
            conditions.append(InvestMomentumEventSnapshot.trading_date == trading_date)
        if surface:
            conditions.append(InvestMomentumEventSnapshot.surface == surface)
        if order_type:
            conditions.append(InvestMomentumEventSnapshot.order_type == order_type)
        if trade_type:
            conditions.append(InvestMomentumEventSnapshot.trade_type == trade_type)

        latest_result = await self._session.execute(
            select(func.max(InvestMomentumEventSnapshot.snapshot_at)).where(*conditions)
        )
        latest_snapshot_at = latest_result.scalar_one_or_none()
        if latest_snapshot_at is None:
            return []

        stmt = (
            select(InvestMomentumEventSnapshot)
            .where(
                *conditions,
                InvestMomentumEventSnapshot.snapshot_at == latest_snapshot_at,
            )
            .order_by(
                InvestMomentumEventSnapshot.rank.asc(),
                InvestMomentumEventSnapshot.symbol.asc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_theme_events(
        self,
        *,
        trading_date: dt.date | None = None,
        event_kind: str | None = None,
        sort_type: str | None = None,
        limit: int = 50,
    ) -> list[InvestThemeEventSnapshot]:
        conditions = [InvestThemeEventSnapshot.market == "kr"]
        if trading_date is not None:
            conditions.append(InvestThemeEventSnapshot.trading_date == trading_date)
        if event_kind:
            conditions.append(InvestThemeEventSnapshot.event_kind == event_kind)
        if sort_type:
            conditions.append(InvestThemeEventSnapshot.sort_type == sort_type)

        latest_result = await self._session.execute(
            select(func.max(InvestThemeEventSnapshot.snapshot_at)).where(*conditions)
        )
        latest_snapshot_at = latest_result.scalar_one_or_none()
        if latest_snapshot_at is None:
            return []

        stmt = (
            select(InvestThemeEventSnapshot)
            .where(
                *conditions, InvestThemeEventSnapshot.snapshot_at == latest_snapshot_at
            )
            .order_by(
                InvestThemeEventSnapshot.rank.asc().nulls_last(),
                InvestThemeEventSnapshot.name.asc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def coverage(self, *, as_of: dt.date) -> SnapshotCoverage:
        momentum_result = await self._session.execute(
            select(
                func.count().label("count"),
                func.max(InvestMomentumEventSnapshot.snapshot_at).label("latest"),
            ).where(
                InvestMomentumEventSnapshot.trading_date == as_of,
                InvestMomentumEventSnapshot.market == "kr",
            )
        )
        theme_result = await self._session.execute(
            select(
                func.count().label("count"),
                func.max(InvestThemeEventSnapshot.snapshot_at).label("latest"),
            ).where(
                InvestThemeEventSnapshot.trading_date == as_of,
                InvestThemeEventSnapshot.market == "kr",
            )
        )
        momentum = momentum_result.one()
        theme = theme_result.one()
        return SnapshotCoverage(
            market="kr",
            as_of=as_of,
            momentum_count=int(momentum.count or 0),
            theme_count=int(theme.count or 0),
            last_momentum_snapshot_at=momentum.latest,
            last_theme_snapshot_at=theme.latest,
        )
