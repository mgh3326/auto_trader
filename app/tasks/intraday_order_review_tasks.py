"""Intraday order review TaskIQ task declarations.

Schedule-facing thin wrappers only. All logic lives in
app/jobs/intraday_order_review.
"""

from __future__ import annotations

from app.core.taskiq_broker import broker
from app.jobs.intraday_order_review import (
    is_kr_trading_hours as _is_kr_trading_hours,
)
from app.jobs.intraday_order_review import (
    is_us_trading_hours as _is_us_trading_hours,
)
from app.jobs.intraday_order_review import (
    run_crypto_order_review,
    run_kr_order_review,
    run_us_order_review,
)

# Re-export private aliases so existing tests importing from this module continue to work.
_is_kr_trading_hours = _is_kr_trading_hours  # noqa: PLW0127
_is_us_trading_hours = _is_us_trading_hours  # noqa: PLW0127


@broker.task(
    schedule=[
        {"cron": "0 14 * * *"},
        {"cron": "0 21 * * *"},
    ],
)
async def intraday_crypto_order_review() -> dict[str, object]:
    """Intraday order review for crypto market (14:00, 21:00 KST)."""
    return await run_crypto_order_review()


@broker.task(
    schedule=[
        {"cron": "0 10 * * 1-5"},
        {"cron": "0 14 * * 1-5"},
    ],
)
async def intraday_kr_order_review() -> dict[str, object]:
    """Intraday order review for Korean stock market (10:00, 14:00 KST, Mon-Fri)."""
    return await run_kr_order_review()


@broker.task(
    schedule=[
        {"cron": "30 0 * * 1-5"},
        {"cron": "0 4 * * 1-5"},
    ],
)
async def intraday_us_order_review() -> dict[str, object]:
    """Intraday order review for US stock market (00:30, 04:00 KST, Mon-Fri)."""
    return await run_us_order_review()


__all__ = [
    "intraday_crypto_order_review",
    "intraday_kr_order_review",
    "intraday_us_order_review",
]
