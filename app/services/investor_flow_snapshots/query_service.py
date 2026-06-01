"""투자자 플로우 read-only read-model query_service (ROB-398 Slice 3).

기존 InvestorFlowSnapshotsRepository 위 thin freshness 래퍼. write 없음.
체결강도(trade strength)는 본 슬라이스 범위 밖.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
FLOW_TTL_DAYS: int = 1  # EOD 일별 데이터 — 당일/전일 fresh


@dataclass(frozen=True)
class InvestorFlowRow:
    symbol: str
    foreign_net: int | None
    institution_net: int | None
    individual_net: int | None
    double_buy: bool
    double_sell: bool
    foreign_consecutive_buy_days: int | None
    foreign_consecutive_sell_days: int | None
    institution_consecutive_buy_days: int | None
    institution_consecutive_sell_days: int | None


@dataclass(frozen=True)
class Freshness:
    overall: str               # "fresh" | "stale" | "unavailable"
    snapshot_date: dt.date | None
    stale_reason: str | None
    age_days: int | None


@dataclass(frozen=True)
class InvestorFlow:
    market: str
    snapshot_date: dt.date | None
    rows: tuple[InvestorFlowRow, ...]
    freshness: Freshness


def _map_row(row: object) -> InvestorFlowRow:
    return InvestorFlowRow(
        symbol=row.symbol,  # type: ignore[union-attr]
        foreign_net=getattr(row, "foreign_net", None),
        institution_net=getattr(row, "institution_net", None),
        individual_net=getattr(row, "individual_net", None),
        double_buy=bool(getattr(row, "double_buy", False)),
        double_sell=bool(getattr(row, "double_sell", False)),
        foreign_consecutive_buy_days=getattr(row, "foreign_consecutive_buy_days", None),
        foreign_consecutive_sell_days=getattr(row, "foreign_consecutive_sell_days", None),
        institution_consecutive_buy_days=getattr(
            row, "institution_consecutive_buy_days", None
        ),
        institution_consecutive_sell_days=getattr(
            row, "institution_consecutive_sell_days", None
        ),
    )


def _derive_freshness(
    rows: Sequence[object], *, now: dt.datetime, ttl_days: int
) -> tuple[Freshness, dt.date | None]:
    if not rows:
        return Freshness("unavailable", None, "no_flow_rows", None), None
    snapshot_date = max(r.snapshot_date for r in rows)  # type: ignore[union-attr]
    age_days = (now.astimezone(_KST).date() - snapshot_date).days
    if age_days <= ttl_days:
        return Freshness("fresh", snapshot_date, None, age_days), snapshot_date
    return Freshness("stale", snapshot_date, "older_than_ttl", age_days), snapshot_date


class InvestorFlowQueryService:
    def __init__(self, repository: object) -> None:
        self._repo = repository

    async def get_investor_flow(
        self,
        *,
        symbols: Iterable[str],
        market: str = "kr",
        now: dt.datetime,
        ttl_days: int = FLOW_TTL_DAYS,
    ) -> InvestorFlow:
        rows = await self._repo.latest_by_symbols(market=market, symbols=list(symbols))  # type: ignore[union-attr]
        freshness, snapshot_date = _derive_freshness(
            rows, now=now, ttl_days=ttl_days
        )
        return InvestorFlow(
            market=market,
            snapshot_date=snapshot_date,
            rows=tuple(_map_row(r) for r in rows),
            freshness=freshness,
        )
