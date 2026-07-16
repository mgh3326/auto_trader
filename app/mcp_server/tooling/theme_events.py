from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    kr_market_data_state,
)
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)

_STALE_AFTER_MINUTES = 20


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _decimal_to_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _stock_to_dict(stock) -> dict[str, Any]:
    return {
        "symbol": stock.symbol,
        "name": stock.name,
        "rank": stock.rank,
        "order_type": stock.order_type,
        "price": _decimal_to_float(stock.price),
        "change_amount": _decimal_to_float(stock.change_amount),
        "change_rate": _decimal_to_float(stock.change_rate),
        "volume": stock.volume,
        "trade_value": _decimal_to_float(stock.trade_value),
    }


def _theme_event_to_dict(row, stocks: list | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "event_kind": row.event_kind,
        "source_event_key": row.source_event_key,
        "naver_theme_no": row.naver_theme_no,
        "naver_upjong_code": row.naver_upjong_code,
        "name": row.name,
        "sort_type": row.sort_type,
        "rank": row.rank,
        "market_type": row.market_type,
        "change_rate": _decimal_to_float(row.change_rate),
        "trade_value": _decimal_to_float(row.trade_value),
        "market_cap": _decimal_to_float(row.market_cap),
        "stock_count": row.stock_count,
        "leader_symbols": row.leader_symbols or [],
    }
    if stocks is not None:
        item["stocks"] = [_stock_to_dict(stock) for stock in stocks]
    return item


def _classify_theme_freshness(
    *, latest_snapshot_at: dt.datetime, now: dt.datetime
) -> str:
    """Intraday staleness: >20min since the last snapshot during a live KRX session.

    The collector cron runs every 10 minutes during KRX regular hours, so a gap
    over 20 minutes means at least one cycle was missed. Outside the regular
    session (pre-market/after-hours/weekend/holiday) the newest persisted
    snapshot is the best available data and is reported as fresh.
    """
    if kr_market_data_state(now) != DATA_STATE_FRESH:
        return "fresh"
    age_minutes = (now - latest_snapshot_at).total_seconds() / 60.0
    if age_minutes > _STALE_AFTER_MINUTES:
        return "stale"
    return "fresh"


async def get_theme_events_impl(
    market: str = "kr",
    event_kind: str = "all",
    top_n: int = 20,
    trading_date: str | None = None,
    at: str | None = None,
    include_stocks: bool = False,
) -> dict[str, Any]:
    """Return read-only 테마/업종 클러스터 snapshots from persisted Naver theme events.

    Wraps InvestMomentumEventSnapshotsRepository.list_theme_events. Never fetches
    Naver or mutates broker/order/watch state.
    """
    top_n = max(1, min(int(top_n or 20), 100))
    if market != "kr":
        return {
            "market": market,
            "event_kind": event_kind,
            "data_state": "unsupported",
            "empty_reason": "naver_stock_supports_kr_only",
            "snapshot_at": None,
            "trading_date": None,
            "items": [],
        }

    snapshot_date = dt.date.fromisoformat(trading_date) if trading_date else None
    at_cutoff = dt.datetime.fromisoformat(at) if at else None
    repo_event_kind = None if event_kind == "all" else event_kind

    async with AsyncSessionLocal() as session:
        repo = InvestMomentumEventSnapshotsRepository(session)
        rows = await repo.list_theme_events(
            trading_date=snapshot_date,
            event_kind=repo_event_kind,
            at=at_cutoff,
            limit=top_n,
        )
        stocks_by_theme_id: dict[int, list] = {}
        if include_stocks and rows:
            stocks_by_theme_id = await repo.list_theme_event_stocks(
                [row.id for row in rows]
            )

    if not rows:
        return {
            "market": "kr",
            "event_kind": event_kind,
            "data_state": "missing",
            "empty_reason": "no_naver_theme_snapshots",
            "snapshot_at": None,
            "trading_date": snapshot_date.isoformat() if snapshot_date else None,
            "items": [],
        }

    latest_snapshot_at = max(row.snapshot_at for row in rows)
    if snapshot_date is None and at_cutoff is None:
        data_state = _classify_theme_freshness(
            latest_snapshot_at=latest_snapshot_at, now=_now_utc()
        )
    else:
        data_state = "fresh"

    return {
        "market": "kr",
        "event_kind": event_kind,
        "data_state": data_state,
        "empty_reason": None,
        "snapshot_at": latest_snapshot_at.isoformat(),
        "trading_date": rows[0].trading_date.isoformat(),
        "items": [
            _theme_event_to_dict(
                row, stocks_by_theme_id.get(row.id) if include_stocks else None
            )
            for row in rows
        ],
    }
