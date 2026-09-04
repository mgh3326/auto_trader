from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_momentum_events.surge_ratio import (
    compute_trade_value_surge_ratio,
)
from app.services.invest_screener_snapshots.freshness import (
    classify_momentum_freshness,
    expected_kr_baseline_date,
)

_SURGE_LOOKBACK_DAYS = 5
_SURGE_TOLERANCE = dt.timedelta(minutes=10)


def _current_trade_value(candidate):
    values = [
        signal["tradeValue"]
        for signal in candidate.signals
        if signal.get("tradeValue") is not None
    ]
    return max(values) if values else None


async def _resolve_surge_ratio(repo, candidate):
    """ROB-919: relative trade-value surge ratio for one candidate.

    Skips the historical-lookback query entirely when there is no current
    trade_value to compare (nothing to divide), which also keeps this a
    no-op against fakes/tests that only implement list_candidate_signals.
    """
    current = _current_trade_value(candidate)
    if current is None:
        return compute_trade_value_surge_ratio(
            current_trade_value=None, historical_trade_values=[]
        )

    historical = await repo.list_historical_trade_values_near_time(
        symbol=candidate.symbol,
        before_date=candidate.trading_date,
        target_time_of_day=candidate.latest_snapshot_at.time(),
        lookback_days=_SURGE_LOOKBACK_DAYS,
        tolerance=_SURGE_TOLERANCE,
    )
    return compute_trade_value_surge_ratio(
        current_trade_value=current, historical_trade_values=historical
    )


def _candidate_to_dict(candidate, surge) -> dict[str, Any]:
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
        "trade_value_surge_ratio": surge.ratio,
        "trade_value_surge_reason": surge.reason_code,
        "trade_value_surge_lookback_days": surge.lookback_days_used,
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
        repo = InvestMomentumEventSnapshotsRepository(session)
        rows = await repo.list_candidate_signals(
            trading_date=snapshot_date,
            limit=limit,
        )
        surges = [await _resolve_surge_ratio(repo, row) for row in rows]
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
        "items": [
            _candidate_to_dict(row, surge)
            for row, surge in zip(rows, surges, strict=True)
        ],
        "scoring_notes": [
            "searchTop/quantTop/up/priceTop 동시 출현을 우대",
            "KRX+NXT 동시 출현과 테마 리더 편입을 보너스로 반영",
            "동일 surface의 직전 스냅샷 대비 순위 개선(rank_delta)을 반영",
            "read-only: 네이버/브로커 요청 없이 저장된 스냅샷만 조회",
            "trade_value_surge_ratio: 당일 누적 거래대금 ÷ 직전 5거래일 동시각 평균 (ROB-919)",
        ],
    }
