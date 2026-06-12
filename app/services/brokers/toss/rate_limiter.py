from __future__ import annotations

import asyncio
import random
import time
from collections import deque
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


class TossRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[TossApiGroup, deque[float]] = {
            group: deque() for group in TossApiGroup
        }
        self._lock = asyncio.Lock()

    @staticmethod
    def limit_for(group: TossApiGroup, *, now: datetime | None = None) -> int:
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        if group in {TossApiGroup.ORDER, TossApiGroup.ORDER_INFO}:
            if now.hour == 9 and 0 <= now.minute < 10:
                return 3
        return _BASE_LIMITS[group]

    async def acquire(self, group: TossApiGroup) -> None:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[group]
            while bucket and now - bucket[0] >= 1.0:
                bucket.popleft()
            limit = self.limit_for(group)
            if len(bucket) >= limit:
                sleep_for = max(1.0 - (now - bucket[0]), 0.0)
                await asyncio.sleep(sleep_for)
                now = time.monotonic()
                while bucket and now - bucket[0] >= 1.0:
                    bucket.popleft()
            bucket.append(now)


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
