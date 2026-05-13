from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from app.services.invest_momentum_events.models import (
    MomentumEventUpsert,
    ThemeEventUpsert,
)
from app.services.naver_stock.parser import (
    parse_domestic_stock_default,
    parse_upjong_theme_list,
)


class NaverMomentumFetcher(Protocol):
    async def fetch_domestic_stock_default(self, **kwargs): ...
    async def fetch_market_theme_list(self, **kwargs): ...
    async def fetch_market_upjong_list(self, **kwargs): ...


@dataclass(frozen=True)
class BuildPlan:
    trade_types: tuple[str, ...] = ("KRX",)
    market_types: tuple[str, ...] = ("ALL",)
    order_types: tuple[str, ...] = ("up", "quantTop", "priceTop", "searchTop")
    theme_sort_types: tuple[str, ...] = ("changeRate",)
    page_size: int = 50


@dataclass(frozen=True)
class BuildPayloads:
    momentum: tuple[MomentumEventUpsert, ...]
    themes: tuple[ThemeEventUpsert, ...]
    counts_by_surface: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _ensure_aware(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.UTC)
    return ts


async def build_payloads_from_naver(
    fetcher: NaverMomentumFetcher,
    *,
    plan: BuildPlan | None = None,
    snapshot_at: dt.datetime | None = None,
    today: dt.date | None = None,
) -> BuildPayloads:
    plan = plan or BuildPlan()
    snapshot = _ensure_aware(snapshot_at or dt.datetime.now(dt.UTC))
    trading_date = today or snapshot.date()
    warnings: list[str] = []
    counts: Counter[str] = Counter()
    momentum: list[MomentumEventUpsert] = []
    themes: list[ThemeEventUpsert] = []

    for trade_type in plan.trade_types:
        for market_type in plan.market_types:
            for order_type in plan.order_types:
                payload = await fetcher.fetch_domestic_stock_default(
                    trade_type=trade_type,
                    market_type=market_type,
                    order_type=order_type,
                    start_idx=0,
                    page_size=plan.page_size,
                )
                parsed = parse_domestic_stock_default(payload)
                warnings.extend(parsed.warnings)
                for row in parsed.rows:
                    momentum.append(
                        MomentumEventUpsert(
                            snapshot_at=snapshot,
                            trading_date=trading_date,
                            surface="domestic_market_stock_default",
                            trade_type=trade_type,
                            market_type=market_type,
                            order_type=order_type,
                            rank=row.rank or 0,
                            symbol=row.symbol,
                            name=row.name,
                            price=row.price,
                            change_amount=row.change_amount,
                            change_rate=row.change_rate,
                            volume=row.volume,
                            trade_value=row.trade_value,
                            market_cap=row.market_cap,
                            raw_payload=row.raw_payload,
                        )
                    )
                counts[f"stock:{trade_type}:{market_type}:{order_type}"] += len(
                    parsed.rows
                )

    for sort_type in plan.theme_sort_types:
        theme_payload = await fetcher.fetch_market_theme_list(
            sort_type=sort_type, start_idx=0, page_size=min(plan.page_size, 100)
        )
        parsed_themes = parse_upjong_theme_list(theme_payload, event_kind="theme")
        warnings.extend(parsed_themes.warnings)
        for row in parsed_themes.rows:
            themes.append(
                ThemeEventUpsert(
                    snapshot_at=snapshot,
                    trading_date=trading_date,
                    surface="market_theme_list",
                    event_kind="theme",
                    source_event_key=f"theme:{row.source_key}:{sort_type}:ALL",
                    naver_theme_no=row.naver_theme_no,
                    name=row.name,
                    sort_type=sort_type,
                    rank=row.rank,
                    market_type="ALL",
                    change_rate=row.change_rate,
                    trade_value=row.trade_value,
                    market_cap=row.market_cap,
                    stock_count=row.stock_count,
                    leader_symbols=list(row.leader_symbols),
                    raw_payload=row.raw_payload,
                )
            )
        counts[f"theme:{sort_type}"] += len(parsed_themes.rows)

        upjong_payload = await fetcher.fetch_market_upjong_list(
            sort_type=sort_type, start_idx=0, page_size=min(plan.page_size, 100)
        )
        parsed_upjong = parse_upjong_theme_list(upjong_payload, event_kind="upjong")
        warnings.extend(parsed_upjong.warnings)
        for row in parsed_upjong.rows:
            themes.append(
                ThemeEventUpsert(
                    snapshot_at=snapshot,
                    trading_date=trading_date,
                    surface="market_upjong_list",
                    event_kind="upjong",
                    source_event_key=f"upjong:{row.source_key}:{sort_type}:ALL",
                    naver_upjong_code=row.naver_upjong_code,
                    name=row.name,
                    sort_type=sort_type,
                    rank=row.rank,
                    market_type="ALL",
                    change_rate=row.change_rate,
                    trade_value=row.trade_value,
                    market_cap=row.market_cap,
                    stock_count=row.stock_count,
                    leader_symbols=list(row.leader_symbols),
                    raw_payload=row.raw_payload,
                )
            )
        counts[f"upjong:{sort_type}"] += len(parsed_upjong.rows)

    return BuildPayloads(
        momentum=tuple(momentum),
        themes=tuple(themes),
        counts_by_surface=dict(counts),
        warnings=tuple(warnings),
    )
