from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.toss import rate_limiter as rate_limiter_module
from app.services.brokers.toss.rate_limiter import (
    TossApiGroup,
    TossRateLimiter,
    get_shared_rate_limiter,
    retry_delay_seconds,
)


def test_order_info_peak_limit_is_three_tps() -> None:
    now = datetime(2026, 6, 12, 9, 5, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 3


def test_order_info_normal_limit_is_six_tps() -> None:
    now = datetime(2026, 6, 12, 9, 11, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 6


def test_market_data_limit_is_ten_tps() -> None:
    limiter = TossRateLimiter()

    assert limiter.limit_for(TossApiGroup.MARKET_DATA) == 10


@pytest.mark.parametrize(
    ("retry_after", "attempt", "expected_min"),
    [("2", 0, 2.0), (None, 2, 4.0), ("bad", 1, 2.0)],
)
def test_retry_delay_seconds_uses_header_or_backoff(
    retry_after: str | None, attempt: int, expected_min: float
) -> None:
    delay = retry_delay_seconds(retry_after, attempt=attempt, jitter=0.0)

    assert delay == expected_min


def test_get_shared_rate_limiter_is_process_singleton() -> None:
    """ROB-547: every call site shares one limiter so group TPS holds across
    concurrent clients within a process."""
    rate_limiter_module.reset_shared_rate_limiter()
    first = get_shared_rate_limiter()
    second = get_shared_rate_limiter()

    assert first is second
    assert isinstance(first, TossRateLimiter)


@pytest.mark.asyncio
async def test_throttled_group_does_not_block_other_groups() -> None:
    """ROB-547: a saturated ORDER bucket must not head-of-line block a
    MARKET_DATA read (per-group locking, sleep outside the global lock)."""
    limiter = TossRateLimiter()
    # Saturate ORDER (peak/normal limit >= 6) so the next ORDER acquire sleeps ~1s.
    for _ in range(TossRateLimiter.limit_for(TossApiGroup.ORDER)):
        await limiter.acquire(TossApiGroup.ORDER)

    async def slow_order() -> None:
        await limiter.acquire(TossApiGroup.ORDER)

    order_task = asyncio.create_task(slow_order())
    await asyncio.sleep(0.05)  # let the ORDER acquire enter its throttle wait

    start = time.monotonic()
    await limiter.acquire(TossApiGroup.MARKET_DATA)
    elapsed = time.monotonic() - start

    assert elapsed < 0.2, "MARKET_DATA was blocked behind the throttled ORDER group"
    order_task.cancel()
