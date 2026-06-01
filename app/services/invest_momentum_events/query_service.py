"""모멘텀(Naver 랭킹) read-only read-model query_service (ROB-398 Slice 2).

기존 InvestMomentumEventsRepository 위 thin 래퍼. freshness 를 명시한다
(ROB-388/389 정직성 계승). write 없음.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
RANKING_TTL_MINUTES: int = 15  # 모멘텀 job 주기 */10 기준


@dataclass(frozen=True)
class RankingRow:
    rank: int
    symbol: str
    name: str | None
    price: float | None
    change_rate: float | None
    volume: int | None
    trade_value: float | None
    market_cap: float | None


@dataclass(frozen=True)
class Freshness:
    overall: str  # "fresh" | "stale" | "unavailable"
    latest_snapshot_at: dt.datetime | None
    stale_reason: str | None


@dataclass(frozen=True)
class MomentumRanking:
    market: str
    order_type: str
    trading_date: dt.date | None
    rows: tuple[RankingRow, ...]
    freshness: Freshness


def _to_float(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _map_row(row: object) -> RankingRow:
    return RankingRow(
        rank=row.rank,
        symbol=row.symbol,
        name=getattr(row, "name", None),
        price=_to_float(getattr(row, "price", None)),
        change_rate=_to_float(getattr(row, "change_rate", None)),
        volume=getattr(row, "volume", None),
        trade_value=_to_float(getattr(row, "trade_value", None)),
        market_cap=_to_float(getattr(row, "market_cap", None)),
    )


def _derive_freshness(
    rows: Sequence[object], *, now: dt.datetime, ttl_minutes: int
) -> tuple[Freshness, dt.date | None]:
    if not rows:
        return Freshness("unavailable", None, "no_ranking_rows"), None
    latest = max(r.snapshot_at for r in rows)
    trading_date = rows[0].trading_date
    if trading_date != now.astimezone(_KST).date():
        return Freshness("stale", latest, "older_trading_date"), trading_date
    if now - latest > dt.timedelta(minutes=ttl_minutes):
        return Freshness("stale", latest, "older_than_ttl"), trading_date
    return Freshness("fresh", latest, None), trading_date


class MomentumRankingQueryService:
    def __init__(self, repository: object) -> None:
        self._repo = repository

    async def get_ranking(
        self,
        *,
        order_type: str = "up",
        market: str = "kr",
        limit: int = 50,
        now: dt.datetime,
        ttl_minutes: int = RANKING_TTL_MINUTES,
    ) -> MomentumRanking:
        rows = await self._repo.list_momentum_events(order_type=order_type, limit=limit)
        freshness, trading_date = _derive_freshness(
            rows, now=now, ttl_minutes=ttl_minutes
        )
        return MomentumRanking(
            market=market,
            order_type=order_type,
            trading_date=trading_date,
            rows=tuple(_map_row(r) for r in rows),
            freshness=freshness,
        )
