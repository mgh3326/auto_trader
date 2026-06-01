from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_screener_snapshots.freshness import (
    classify_momentum_freshness,
    expected_kr_baseline_date,
)


def _candidate_to_dict(candidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "name": candidate.name,
        "score": candidate.score,
        "latest_snapshot_at": candidate.latest_snapshot_at.isoformat(),
        "trading_date": candidate.trading_date.isoformat(),
        "price": float(candidate.price) if candidate.price is not None else None,
        "change_rate": float(candidate.change_rate)
        if candidate.change_rate is not None
        else None,
        "surface_count": candidate.surface_count,
        "venue_count": candidate.venue_count,
        "rank_delta": candidate.rank_delta,
        "signals": [
            {
                **signal,
                "changeRate": float(signal["changeRate"])
                if signal.get("changeRate") is not None
                else None,
                "tradeValue": float(signal["tradeValue"])
                if signal.get("tradeValue") is not None
                else None,
            }
            for signal in candidate.signals
        ],
        "theme_names": candidate.theme_names,
        "reason_codes": candidate.reason_codes,
    }


async def get_momentum_candidates_impl(
    market: str = "kr",
    date: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return read-only 급등 조기 포착 candidates from persisted Naver snapshots."""
    limit = max(1, min(int(limit or 20), 50))
    if market != "kr":
        return {
            "market": market,
            "data_state": "unsupported",
            "empty_reason": "naver_stock_supports_kr_only",
            "items": [],
        }

    snapshot_date = dt.date.fromisoformat(date) if date else None
    async with AsyncSessionLocal() as session:
        rows = await InvestMomentumEventSnapshotsRepository(
            session
        ).list_candidate_signals(
            trading_date=snapshot_date,
            limit=limit,
        )
    now = dt.datetime.now(dt.UTC)
    if rows:
        latest_trading_date = rows[0].trading_date
        data_state, days_stale = classify_momentum_freshness(
            latest_trading_date=latest_trading_date, now=now
        )
        empty_reason = None
    else:
        latest_trading_date = None
        data_state, days_stale = "missing", 0
        empty_reason = "no_naver_momentum_snapshots"

    return {
        "market": "kr",
        "data_state": data_state,
        "days_stale": days_stale,
        "expected_baseline_date": expected_kr_baseline_date(now).isoformat(),
        "latest_trading_date": (
            latest_trading_date.isoformat() if latest_trading_date else None
        ),
        "empty_reason": empty_reason,
        "items": [_candidate_to_dict(row) for row in rows],
        "scoring_notes": [
            "searchTop/quantTop/up/priceTop 동시 출현을 우대",
            "KRX+NXT 동시 출현과 테마 리더 편입을 보너스로 반영",
            "동일 surface의 직전 스냅샷 대비 순위 개선(rank_delta)을 반영",
            "read-only: 네이버/브로커 요청 없이 저장된 스냅샷만 조회",
        ],
    }
