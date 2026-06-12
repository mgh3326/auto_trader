from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.toss.rate_limiter import (
    TossApiGroup,
    TossRateLimiter,
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
