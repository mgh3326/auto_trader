"""Dry-run-default job boundary for Naver momentum/theme event snapshots."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events.builder import (
    BuildPlan,
    build_payloads_from_naver,
)
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.naver_stock.client import NaverStockClient


@dataclass(frozen=True)
class NaverMomentumBuildRequest:
    trade_types: tuple[str, ...] = ("KRX",)
    market_types: tuple[str, ...] = ("ALL",)
    order_types: tuple[str, ...] = ("up", "quantTop", "priceTop", "searchTop")
    theme_sort_types: tuple[str, ...] = ("changeRate",)
    page_size: int = 50
    commit: bool = False
    today: dt.date | None = None


@dataclass(frozen=True)
class NaverMomentumBuildResult:
    momentum_rows: int
    theme_rows: int
    committed: bool
    counts_by_surface: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    samples: tuple[dict[str, str | int | None], ...] = ()


async def run_naver_momentum_build(
    request: NaverMomentumBuildRequest | None = None, *, fetcher=None
) -> NaverMomentumBuildResult:
    request = request or NaverMomentumBuildRequest()
    should_commit = bool(
        request.commit and settings.invest_momentum_events_commit_enabled
    )
    client = fetcher or NaverStockClient()
    close_client = fetcher is None
    try:
        payloads = await build_payloads_from_naver(
            client,
            plan=BuildPlan(
                trade_types=request.trade_types,
                market_types=request.market_types,
                order_types=request.order_types,
                theme_sort_types=request.theme_sort_types,
                page_size=request.page_size,
            ),
            today=request.today,
        )
    finally:
        if close_client and hasattr(client, "aclose"):
            await client.aclose()

    if should_commit:
        async with AsyncSessionLocal() as session:
            repo = InvestMomentumEventSnapshotsRepository(session)
            for row in payloads.momentum:
                await repo.upsert_momentum(row)
            for row in payloads.themes:
                await repo.upsert_theme(row)
            await session.commit()

    samples = tuple(
        {
            "symbol": row.symbol,
            "name": row.name,
            "rank": row.rank,
            "orderType": row.order_type,
        }
        for row in payloads.momentum[:10]
    )
    warnings = payloads.warnings
    if request.commit and not settings.invest_momentum_events_commit_enabled:
        warnings = (
            *warnings,
            "commit requested but invest_momentum_events_commit_enabled is false; dry-run only",
        )
    return NaverMomentumBuildResult(
        momentum_rows=len(payloads.momentum),
        theme_rows=len(payloads.themes),
        committed=should_commit,
        counts_by_surface=payloads.counts_by_surface,
        warnings=warnings,
        samples=samples,
    )
