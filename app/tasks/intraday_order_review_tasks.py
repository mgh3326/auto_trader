"""Background tasks for intraday order review."""

from __future__ import annotations

import logging
from datetime import datetime

from app.core.taskiq_broker import broker
from app.core.timezone import now_kst
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)


@broker.task(
    schedule=[
        {"cron": "0 14 * * *"},
        {"cron": "0 21 * * *"},
    ],
)
async def intraday_crypto_order_review() -> dict[str, object]:
    """Intraday order review for crypto market (14:00, 21:00 KST)."""
    as_of = now_kst()
    logger.info(f"Starting intraday crypto order review at {as_of}")

    result = await fetch_pending_orders(
        market="crypto",
        include_current_price=True,
        include_indicators=True,
        as_of=as_of,
    )

    order_count = result.get("summary", {}).get("total", 0)
    logger.info(f"Crypto intraday review complete: {order_count} pending orders")

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


@broker.task(
    schedule=[
        {"cron": "0 10 * * 1-5"},
        {"cron": "0 14 * * 1-5"},
    ],
)
async def intraday_kr_order_review() -> dict[str, object]:
    """Intraday order review for Korean stock market (10:00, 14:00 KST, Mon-Fri)."""
    as_of = now_kst()

    if not _is_kr_trading_hours(as_of):
        logger.info("Skipping KR intraday review: outside trading hours")
        return {"market": "kr", "skipped": True, "reason": "outside_trading_hours"}

    logger.info(f"Starting intraday KR order review at {as_of}")

    result = await fetch_pending_orders(
        market="kr",
        include_current_price=True,
        include_indicators=False,
        as_of=as_of,
    )

    order_count = result.get("summary", {}).get("total", 0)
    logger.info(f"KR intraday review complete: {order_count} pending orders")

    return {
        "market": "kr",
        "as_of": as_of.isoformat(),
        "order_count": order_count,
    }


@broker.task(
    schedule=[
        {"cron": "30 0 * * 1-5"},
        {"cron": "0 4 * * 1-5"},
    ],
)
async def intraday_us_order_review() -> dict[str, object]:
    """Intraday order review for US stock market (00:30, 04:00 KST, Mon-Fri)."""
    as_of = now_kst()

    if not _is_us_trading_hours(as_of):
        logger.info("Skipping US intraday review: outside trading hours")
        return {"market": "us", "skipped": True, "reason": "outside_trading_hours"}

    logger.info(f"Starting intraday US order review at {as_of}")

    result = await fetch_pending_orders(
        market="us",
        include_current_price=True,
        include_indicators=False,
        as_of=as_of,
    )

    order_count = result.get("summary", {}).get("total", 0)
    logger.info(f"US intraday review complete: {order_count} pending orders")

    return {
        "market": "us",
        "as_of": as_of.isoformat(),
        "order_count": order_count,
    }


def _is_kr_trading_hours(dt: datetime) -> bool:
    """Check if current time is within KR market hours (09:00-15:30 KST)."""
    if dt.weekday() >= 5:
        return False
    hour = dt.hour
    minute = dt.minute
    time_val = hour * 100 + minute
    return 900 <= time_val <= 1530


def _is_us_trading_hours(dt: datetime) -> bool:
    """Check if current time is within US market hours (23:30-06:00 KST)."""
    if dt.weekday() >= 5:
        return False
    hour = dt.hour
    return hour >= 23 or hour < 6


__all__ = [
    "intraday_crypto_order_review",
    "intraday_kr_order_review",
    "intraday_us_order_review",
]
