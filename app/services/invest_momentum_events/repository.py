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
class MomentumCandidateSignal:
    symbol: str
    name: str | None
    score: float
    latest_snapshot_at: dt.datetime
    trading_date: dt.date
    price: object | None
    change_rate: object | None
    surface_count: int
    venue_count: int
    rank_delta: int | None
    signals: list[dict]
    theme_names: list[str]
    reason_codes: list[str]


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

    async def list_candidate_signals(
        self,
        *,
        trading_date: dt.date | None = None,
        limit: int = 20,
    ) -> list[MomentumCandidateSignal]:
        """Score latest persisted Naver momentum snapshots into early-catch candidates.

        Read-only. The scoring intentionally favors cross-surface confirmation:
        searchTop + quantTop + up + KRX/NXT repetition is stronger than a single
        price-rank appearance. Rank deltas are computed against the prior same-day
        snapshot for the same symbol/surface/venue/order combination when present.
        """
        conditions = [InvestMomentumEventSnapshot.market == "kr"]
        if trading_date is not None:
            conditions.append(InvestMomentumEventSnapshot.trading_date == trading_date)

        latest_result = await self._session.execute(
            select(func.max(InvestMomentumEventSnapshot.snapshot_at)).where(*conditions)
        )
        latest_snapshot_at = latest_result.scalar_one_or_none()
        if latest_snapshot_at is None:
            return []

        if trading_date is None:
            date_result = await self._session.execute(
                select(InvestMomentumEventSnapshot.trading_date)
                .where(
                    InvestMomentumEventSnapshot.snapshot_at == latest_snapshot_at,
                    InvestMomentumEventSnapshot.market == "kr",
                )
                .limit(1)
            )
            trading_date = date_result.scalar_one()

        rows_result = await self._session.execute(
            select(InvestMomentumEventSnapshot)
            .where(
                InvestMomentumEventSnapshot.market == "kr",
                InvestMomentumEventSnapshot.trading_date == trading_date,
            )
            .order_by(
                InvestMomentumEventSnapshot.snapshot_at.asc(),
                InvestMomentumEventSnapshot.rank.asc(),
            )
        )
        rows = list(rows_result.scalars().all())
        latest_rows = [row for row in rows if row.snapshot_at == latest_snapshot_at]
        if not latest_rows:
            return []

        previous_ranks: dict[tuple[str, str | None, str | None, str], int] = {}
        for row in rows:
            if row.snapshot_at >= latest_snapshot_at:
                continue
            key = (row.symbol, row.trade_type, row.market_type, row.order_type)
            previous_ranks[key] = row.rank

        latest_theme_result = await self._session.execute(
            select(func.max(InvestThemeEventSnapshot.snapshot_at)).where(
                InvestThemeEventSnapshot.market == "kr",
                InvestThemeEventSnapshot.trading_date == trading_date,
            )
        )
        latest_theme_snapshot_at = latest_theme_result.scalar_one_or_none()
        themes_by_symbol: dict[str, list[str]] = {}
        if latest_theme_snapshot_at is not None:
            theme_rows_result = await self._session.execute(
                select(InvestThemeEventSnapshot, InvestThemeEventSnapshotStock)
                .join(
                    InvestThemeEventSnapshotStock,
                    InvestThemeEventSnapshotStock.theme_snapshot_id
                    == InvestThemeEventSnapshot.id,
                )
                .where(
                    InvestThemeEventSnapshot.market == "kr",
                    InvestThemeEventSnapshot.trading_date == trading_date,
                    InvestThemeEventSnapshot.snapshot_at == latest_theme_snapshot_at,
                )
            )
            for theme, stock in theme_rows_result.all():
                bucket = themes_by_symbol.setdefault(stock.symbol, [])
                if theme.name not in bucket:
                    bucket.append(theme.name)

        order_weights = {
            "searchTop": 34.0,
            "quantTop": 29.0,
            "up": 27.0,
            "priceTop": 18.0,
        }
        grouped: dict[str, dict] = {}
        for row in latest_rows:
            bucket = grouped.setdefault(
                row.symbol,
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "price": row.price,
                    "change_rate": row.change_rate,
                    "score": 0.0,
                    "surfaces": set(),
                    "venues": set(),
                    "signals": [],
                    "rank_deltas": [],
                    "reason_codes": set(),
                },
            )
            if not bucket.get("name") and row.name:
                bucket["name"] = row.name
            if row.price is not None:
                bucket["price"] = row.price
            if row.change_rate is not None:
                bucket["change_rate"] = row.change_rate

            rank_score = max(0.0, 21.0 - float(row.rank))
            order_score = order_weights.get(row.order_type, 12.0)
            score_add = order_score + rank_score
            if row.change_rate is not None:
                score_add += min(max(float(row.change_rate), 0.0), 20.0)

            key = (row.symbol, row.trade_type, row.market_type, row.order_type)
            prev_rank = previous_ranks.get(key)
            rank_delta = prev_rank - row.rank if prev_rank is not None else None
            if rank_delta is not None and rank_delta > 0:
                score_add += min(float(rank_delta) * 1.5, 30.0)
                bucket["rank_deltas"].append(rank_delta)
                bucket["reason_codes"].add("rank_improving")

            bucket["score"] += score_add
            bucket["surfaces"].add(row.order_type)
            if row.trade_type:
                bucket["venues"].add(row.trade_type)
            bucket["signals"].append(
                {
                    "orderType": row.order_type,
                    "tradeType": row.trade_type,
                    "rank": row.rank,
                    "rankDelta": rank_delta,
                    "changeRate": row.change_rate,
                    "volume": row.volume,
                    "tradeValue": row.trade_value,
                }
            )
            bucket["reason_codes"].add(f"surface_{row.order_type}")

        candidates: list[MomentumCandidateSignal] = []
        for symbol, bucket in grouped.items():
            surface_count = len(bucket["surfaces"])
            venue_count = len(bucket["venues"])
            if surface_count >= 2:
                bucket["score"] += 20.0 * (surface_count - 1)
                bucket["reason_codes"].add("multi_surface")
            if venue_count >= 2:
                bucket["score"] += 12.0
                bucket["reason_codes"].add("krx_nxt_confirmed")
            theme_names = themes_by_symbol.get(symbol, [])
            if theme_names:
                bucket["score"] += 10.0
                bucket["reason_codes"].add("theme_leader")
            rank_delta = max(bucket["rank_deltas"]) if bucket["rank_deltas"] else None
            candidates.append(
                MomentumCandidateSignal(
                    symbol=symbol,
                    name=bucket["name"],
                    score=round(float(bucket["score"]), 2),
                    latest_snapshot_at=latest_snapshot_at,
                    trading_date=trading_date,
                    price=bucket["price"],
                    change_rate=bucket["change_rate"],
                    surface_count=surface_count,
                    venue_count=venue_count,
                    rank_delta=rank_delta,
                    signals=sorted(
                        bucket["signals"],
                        key=lambda item: (
                            item["rank"],
                            item["orderType"],
                            item.get("tradeType") or "",
                        ),
                    ),
                    theme_names=theme_names[:5],
                    reason_codes=sorted(bucket["reason_codes"]),
                )
            )

        candidates.sort(key=lambda item: (-item.score, item.symbol))
        return candidates[:limit]

    async def list_theme_events(
        self,
        *,
        trading_date: dt.date | None = None,
        event_kind: str | None = None,
        sort_type: str | None = None,
        at: dt.datetime | None = None,
        limit: int = 50,
    ) -> list[InvestThemeEventSnapshot]:
        conditions = [InvestThemeEventSnapshot.market == "kr"]
        if trading_date is not None:
            conditions.append(InvestThemeEventSnapshot.trading_date == trading_date)
        if event_kind:
            conditions.append(InvestThemeEventSnapshot.event_kind == event_kind)
        if sort_type:
            conditions.append(InvestThemeEventSnapshot.sort_type == sort_type)
        if at is not None:
            conditions.append(InvestThemeEventSnapshot.snapshot_at <= at)

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

    async def list_theme_event_stocks(
        self, theme_snapshot_ids: list[int]
    ) -> dict[int, list[InvestThemeEventSnapshotStock]]:
        """Fetch child leader-stock rows for the given theme snapshot ids, grouped by parent."""
        if not theme_snapshot_ids:
            return {}
        result = await self._session.execute(
            select(InvestThemeEventSnapshotStock)
            .where(
                InvestThemeEventSnapshotStock.theme_snapshot_id.in_(theme_snapshot_ids)
            )
            .order_by(
                InvestThemeEventSnapshotStock.theme_snapshot_id.asc(),
                InvestThemeEventSnapshotStock.rank.asc().nulls_last(),
            )
        )
        grouped: dict[int, list[InvestThemeEventSnapshotStock]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.theme_snapshot_id, []).append(row)
        return grouped

    async def list_recent_trading_dates(
        self,
        *,
        before_date: dt.date,
        limit: int = 5,
    ) -> list[dt.date]:
        """Distinct KR trading dates strictly before ``before_date``, most recent first."""
        result = await self._session.execute(
            select(InvestMomentumEventSnapshot.trading_date)
            .where(
                InvestMomentumEventSnapshot.market == "kr",
                InvestMomentumEventSnapshot.trading_date < before_date,
            )
            .distinct()
            .order_by(InvestMomentumEventSnapshot.trading_date.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_symbol_trade_value_near_time(
        self,
        *,
        symbol: str,
        trading_date: dt.date,
        target_at: dt.datetime,
        tolerance: dt.timedelta,
    ) -> object | None:
        """Closest-in-time ``trade_value`` observation for ``symbol`` on
        ``trading_date`` within ``tolerance`` of ``target_at``.

        Multiple surfaces (order_type) can be captured a few seconds apart
        under the same 10-minute snapshot_at bucket; the max trade_value
        among rows at the chosen closest snapshot_at is used since trade
        value is cumulative-for-the-day and should not decrease.
        """
        result = await self._session.execute(
            select(
                InvestMomentumEventSnapshot.snapshot_at,
                InvestMomentumEventSnapshot.trade_value,
            ).where(
                InvestMomentumEventSnapshot.market == "kr",
                InvestMomentumEventSnapshot.symbol == symbol,
                InvestMomentumEventSnapshot.trading_date == trading_date,
                InvestMomentumEventSnapshot.snapshot_at >= target_at - tolerance,
                InvestMomentumEventSnapshot.snapshot_at <= target_at + tolerance,
            )
        )
        rows = result.all()
        if not rows:
            return None

        closest_at = min(
            (row.snapshot_at for row in rows), key=lambda at: abs(at - target_at)
        )
        values_at_closest = [
            row.trade_value
            for row in rows
            if row.snapshot_at == closest_at and row.trade_value is not None
        ]
        if not values_at_closest:
            return None
        return max(values_at_closest)

    async def list_historical_trade_values_near_time(
        self,
        *,
        symbol: str,
        before_date: dt.date,
        target_time_of_day: dt.time,
        lookback_days: int = 5,
        tolerance: dt.timedelta = dt.timedelta(minutes=10),
    ) -> list[object | None]:
        """Same-time-of-day ``trade_value`` for ``symbol`` on the
        ``lookback_days`` most recent trading dates before ``before_date``,
        most-recent-first. ``target_time_of_day`` is UTC (i.e. already
        converted from KST by the caller) since snapshots are stored in UTC.
        A day with no near-time observation yields ``None`` at that position
        -- positions are aligned with the trading dates, gaps are not
        collapsed, so callers can tell "no data that day" apart from "fewer
        days exist".
        """
        trading_dates = await self.list_recent_trading_dates(
            before_date=before_date, limit=lookback_days
        )
        values: list[object | None] = []
        for trading_date in trading_dates:
            target_at = dt.datetime.combine(
                trading_date, target_time_of_day, tzinfo=dt.UTC
            )
            values.append(
                await self.get_symbol_trade_value_near_time(
                    symbol=symbol,
                    trading_date=trading_date,
                    target_at=target_at,
                    tolerance=tolerance,
                )
            )
        return values

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
