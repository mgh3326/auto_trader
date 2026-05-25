"""Deterministic Fundamentals dimension evidence bundle (ROB-311).

Assembles per-symbol valuation (PER/PBR/ROE/dividend/market_cap/52w from
``market_valuation_snapshots``) + sector (``stock_info``) into a market+symbol
bundle, mirroring ``market_evidence``/``news_evidence``. DB-ONLY — never calls a
live broker API. ``market_valuation_snapshots`` is empty until ingestion is
enabled (operator gate); this degrades to ``unavailable``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from decimal import Decimal
from typing import Any

from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)
from app.services.stock_info_service import StockInfoService

FRESH_WINDOW_DAYS = 7


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def build_fundamentals_evidence(
    valuation_repo: MarketValuationSnapshotsRepository,
    stock_info_service: StockInfoService,
    *,
    market: str,
    symbols: Set[str],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or dt.datetime.now(tz=dt.UTC)
    requested = len(symbols)
    rows = await valuation_repo.latest_for_symbols(market=market, symbols=set(symbols))

    per_symbol: list[dict[str, Any]] = []
    latest_date: dt.date | None = None
    for row in rows:
        info = await stock_info_service.get_stock_info_by_symbol(row.symbol)
        per_symbol.append(
            {
                "symbol": row.symbol,
                "sector": getattr(info, "sector", None),
                "per": _f(row.per),
                "pbr": _f(row.pbr),
                "roe": _f(row.roe),
                "dividend_yield": _f(row.dividend_yield),
                "market_cap": _f(row.market_cap),
                "high_52w": _f(row.high_52w),
                "low_52w": _f(row.low_52w),
            }
        )
        if latest_date is None or row.snapshot_date > latest_date:
            latest_date = row.snapshot_date

    if not per_symbol:
        status = "unavailable"
    elif latest_date is not None and latest_date >= (
        now_dt.date() - dt.timedelta(days=FRESH_WINDOW_DAYS)
    ):
        status = "fresh"
    else:
        status = "stale"

    return {
        "market": market,
        "per_symbol": per_symbol,
        "covered_count": len(per_symbol),
        "freshness": {
            "status": status,
            "latest_snapshot_date": latest_date.isoformat() if latest_date else None,
        },
        "data_health": {"requested": requested, "covered": len(per_symbol)},
    }
