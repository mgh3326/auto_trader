from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo


class TossApiGroup(StrEnum):
    AUTH = "AUTH"
    ACCOUNT = "ACCOUNT"
    ASSET = "ASSET"
    STOCK = "STOCK"
    MARKET_INFO = "MARKET_INFO"
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"
    ORDER = "ORDER"
    ORDER_HISTORY = "ORDER_HISTORY"
    ORDER_INFO = "ORDER_INFO"


_BASE_LIMITS: dict[TossApiGroup, int] = {
    TossApiGroup.AUTH: 5,
    TossApiGroup.ACCOUNT: 1,
    TossApiGroup.ASSET: 5,
    TossApiGroup.STOCK: 5,
    TossApiGroup.MARKET_INFO: 3,
    TossApiGroup.MARKET_DATA: 10,
    TossApiGroup.MARKET_DATA_CHART: 5,
    TossApiGroup.ORDER: 6,
    TossApiGroup.ORDER_HISTORY: 5,
    TossApiGroup.ORDER_INFO: 6,
}


@dataclass(frozen=True)
class TossRateLimitHeaders:
    """Rate-limit metadata supplied by the Toss Open API response.

    The provider may change limits without notice.  Consumers that need a
    current cap must use ``limit`` from this response snapshot, not
    ``_BASE_LIMITS`` (which remains only a local, process-level guard for the
    established runtime clients).
    """

    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    retry_after_seconds: float | None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header case-insensitively for both httpx and plain test mappings."""

    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _nonnegative_float(value: str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed >= 0.0 else None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> TossRateLimitHeaders:
    """Parse the documented Toss rate headers without inventing a fallback cap."""

    return TossRateLimitHeaders(
        limit=_positive_int(_header_value(headers, "X-RateLimit-Limit")),
        remaining=_nonnegative_int(_header_value(headers, "X-RateLimit-Remaining")),
        reset_seconds=_nonnegative_float(_header_value(headers, "X-RateLimit-Reset")),
        retry_after_seconds=_nonnegative_float(_header_value(headers, "Retry-After")),
    )


class TossRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[TossApiGroup, deque[float]] = {
            group: deque() for group in TossApiGroup
        }
        # Per-group locks so a throttled group never head-of-line blocks another.
        self._locks: dict[TossApiGroup, asyncio.Lock] = {
            group: asyncio.Lock() for group in TossApiGroup
        }

    @staticmethod
    def limit_for(group: TossApiGroup, *, now: datetime | None = None) -> int:
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        if group in {TossApiGroup.ORDER, TossApiGroup.ORDER_INFO}:
            if now.hour == 9 and 0 <= now.minute < 10:
                return 3
        return _BASE_LIMITS[group]

    async def acquire(self, group: TossApiGroup) -> None:
        lock = self._locks[group]
        bucket = self._buckets[group]
        while True:
            async with lock:
                now = time.monotonic()
                while bucket and now - bucket[0] >= 1.0:
                    bucket.popleft()
                # Re-evaluate the limit each iteration so the 6->3 peak-window
                # transition cannot admit an extra call.
                limit = self.limit_for(group)
                if len(bucket) < limit:
                    bucket.append(now)
                    return
                sleep_for = max(1.0 - (now - bucket[0]), 0.0)
            # Sleep OUTSIDE the lock, then re-loop and re-check the limit.
            await asyncio.sleep(sleep_for if sleep_for > 0.0 else 0.001)


_shared_rate_limiter: TossRateLimiter | None = None


def get_shared_rate_limiter() -> TossRateLimiter:
    """Process-global limiter shared by every Toss client/token manager built via
    ``from_settings``, so group TPS budgets hold across concurrent call sites."""
    global _shared_rate_limiter
    if _shared_rate_limiter is None:
        _shared_rate_limiter = TossRateLimiter()
    return _shared_rate_limiter


def reset_shared_rate_limiter() -> None:
    """Test hook: drop the process-global limiter so suites start clean."""
    global _shared_rate_limiter
    _shared_rate_limiter = None


def retry_delay_seconds(
    retry_after: str | None, *, attempt: int, jitter: float | None = None
) -> float:
    try:
        if retry_after is not None:
            return max(float(retry_after), 0.0)
    except ValueError:
        pass
    base = min(2.0**attempt, 16.0)
    return base + (random.uniform(0.0, base) if jitter is None else jitter)
