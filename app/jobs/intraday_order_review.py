"""Intraday order review orchestration — scheduler-agnostic.

Trading hours helpers and market-specific review logic live here.
TaskIQ schedule declarations belong in app/tasks/intraday_order_review_tasks.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.timezone import now_kst
from app.services.market_events.session_calendar import regular_session_bounds
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _as_kst(dt_value: datetime) -> datetime:
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=_KST)
    return dt_value.astimezone(_KST)


def _within_utc_bounds(dt_value: datetime, bounds: tuple[datetime, datetime]) -> bool:
    as_utc = _as_kst(dt_value).astimezone(UTC)
    start, end = bounds
    return start <= as_utc < end


def is_kr_trading_hours(dt_value: datetime) -> bool:
    """Return True if dt is within the XKRX regular session."""
    local = _as_kst(dt_value)
    bounds = regular_session_bounds("kr", local.date())
    return bounds is not None and _within_utc_bounds(local, bounds)


def is_us_trading_hours(dt_value: datetime) -> bool:
    """Return True if dt is within the XNYS regular session, DST/holiday aware."""
    local = _as_kst(dt_value)
    et_day = local.astimezone(_ET).date()
    bounds = regular_session_bounds("us", et_day)
    return bounds is not None and _within_utc_bounds(local, bounds)


async def run_crypto_order_review() -> dict[str, object]:
    """Review pending crypto orders at scheduled intraday checkpoints."""
    as_of = now_kst()
    logger.info("Starting intraday crypto order review at %s", as_of)
    result = await fetch_pending_orders(
        market="crypto",
        include_current_price=True,
        include_indicators=True,
        as_of=as_of,
    )
    order_count = result.get("summary", {}).get("total", 0)
    logger.info("Crypto intraday review complete: %s pending orders", order_count)
    return {
        "market": "crypto",
        "as_of": as_of.isoformat(),
        "order_count": order_count,
        "orders": [
            {
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "gap_pct": order.get("gap_pct"),
                "indicators": order.get("indicators"),
            }
            for order in result.get("orders", [])
        ],
    }


async def run_kr_order_review() -> dict[str, object]:
    """Review pending KR stock orders at scheduled intraday checkpoints."""
    as_of = now_kst()
    if not is_kr_trading_hours(as_of):
        logger.info("Skipping KR intraday review: outside trading hours")
        return {"market": "kr", "skipped": True, "reason": "outside_trading_hours"}
    logger.info("Starting intraday KR order review at %s", as_of)
    result = await fetch_pending_orders(
        market="kr",
        include_current_price=True,
        include_indicators=False,
        as_of=as_of,
    )
    order_count = result.get("summary", {}).get("total", 0)
    logger.info("KR intraday review complete: %s pending orders", order_count)
    return {
        "market": "kr",
        "as_of": as_of.isoformat(),
        "order_count": order_count,
    }


async def run_us_order_review() -> dict[str, object]:
    """Review pending US stock orders at scheduled intraday checkpoints."""
    as_of = now_kst()
    if not is_us_trading_hours(as_of):
        logger.info("Skipping US intraday review: outside trading hours")
        return {"market": "us", "skipped": True, "reason": "outside_trading_hours"}
    logger.info("Starting intraday US order review at %s", as_of)
    result = await fetch_pending_orders(
        market="us",
        include_current_price=True,
        include_indicators=False,
        as_of=as_of,
    )
    order_count = result.get("summary", {}).get("total", 0)
    logger.info("US intraday review complete: %s pending orders", order_count)
    return {
        "market": "us",
        "as_of": as_of.isoformat(),
        "order_count": order_count,
    }
