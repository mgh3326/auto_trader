"""Deterministic Market dimension evidence bundle (ROB-306).

Assembles breadth + top movers + freshness from the populated KR/US
``invest_screener_snapshots`` (reusing ROB-304 ``screener_evidence``). No prose,
no LLM — this is the raw material Hermes reads to write the Market report.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set
from typing import Any

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)
from app.services.screener_evidence import build_candidate_evidence

TOP_MOVERS_N = 10


async def build_market_evidence(
    repo: InvestScreenerSnapshotsRepository,
    *,
    market: str,
    held: Set[str] = frozenset(),
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    today = (now or dt.datetime.now(tz=dt.UTC)).date()
    coverage = await repo.coverage(market=market, today_trading_date=today)
    breadth = await repo.breadth(market=market)
    rows = await repo.list_top_candidates(market=market, limit=TOP_MOVERS_N)
    evidence = build_candidate_evidence(
        market=market,
        preset="top_gainers",
        rows=[
            {
                "symbol": r.symbol,
                "name": r.symbol,
                "source": r.source,
                "change_rate": r.change_rate,
                "price": r.latest_close,
                "daily_volume": r.daily_volume,
                "consecutive_up_days": r.consecutive_up_days,
            }
            for r in rows
        ],
    )
    top_movers = []
    for ev in evidence:
        d = ev.to_payload_dict()
        d["is_held"] = ev.symbol in held
        top_movers.append(d)

    if coverage.fresh_count > 0:
        freshness_status = "fresh"
    elif coverage.stale_count > 0:
        freshness_status = "stale"
    else:
        freshness_status = "missing"

    return {
        "market": market,
        "breadth": {
            "total": breadth.total,
            "advancers": breadth.advancers,
            "decliners": breadth.decliners,
            "unchanged": breadth.unchanged,
            "advancer_ratio": round(breadth.advancers / breadth.total, 4)
            if breadth.total
            else None,
        },
        "top_movers": top_movers,
        "held_in_market": [m["symbol"] for m in top_movers if m["is_held"]],
        "freshness": {
            "partition_date": breadth.partition_date.isoformat()
            if breadth.partition_date
            else None,
            "status": freshness_status,
            "last_computed_at": coverage.last_computed_at.isoformat()
            if coverage.last_computed_at
            else None,
        },
        "data_health": {
            "fresh_count": coverage.fresh_count,
            "stale_count": coverage.stale_count,
        },
    }
