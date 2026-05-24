"""Deterministic Sentiment dimension evidence bundle (ROB-312).

Assembles per-symbol KR investor-flow consensus (foreign/institution net,
double_buy/sell, consecutive-buy streaks from ``investor_flow_snapshots``) into
a market+symbol bundle, mirroring ``fundamentals_evidence``. DB-ONLY, no LLM.

KR-ONLY: investor-flow data is KR-only (``market IN ('kr')``). For non-KR
markets the assembler returns ``unavailable`` (no query). ``investor_flow_
snapshots`` is populated (ROB-205), unlike the other dimensions' sources.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from typing import Any

from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)

FRESH_WINDOW_DAYS = 5


def _unavailable(market: str, requested: int) -> dict[str, Any]:
    return {
        "market": market,
        "per_symbol": [],
        "covered_count": 0,
        "freshness": {"status": "unavailable", "latest_snapshot_date": None},
        "data_health": {"requested": requested, "covered": 0},
    }


async def build_sentiment_evidence(
    flow_repo: InvestorFlowSnapshotsRepository,
    *,
    market: str,
    symbols: Set[str],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    requested = len(symbols)
    # KR-only source (investor_flow_snapshots.market IN ('kr')). Non-KR markets
    # have no distinct DB sentiment signal yet → unavailable (no query).
    if market.strip().lower() != "kr":
        return _unavailable(market, requested)

    now_dt = now or dt.datetime.now(tz=dt.UTC)
    rows = await flow_repo.latest_by_symbols(market="kr", symbols=set(symbols))

    per_symbol: list[dict[str, Any]] = []
    latest_date: dt.date | None = None
    for row in rows:
        per_symbol.append(
            {
                "symbol": row.symbol,
                "foreign_net": row.foreign_net,
                "institution_net": row.institution_net,
                "double_buy": bool(row.double_buy),
                "double_sell": bool(row.double_sell),
                "foreign_consecutive_buy_days": row.foreign_consecutive_buy_days,
                "institution_consecutive_buy_days": (
                    row.institution_consecutive_buy_days
                ),
            }
        )
        if latest_date is None or row.snapshot_date > latest_date:
            latest_date = row.snapshot_date

    if not per_symbol:
        return _unavailable(market, requested)

    if latest_date is not None and latest_date >= (
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
