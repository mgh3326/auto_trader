"""Resident ``quotes:toss`` consumer entry points (#1120, records only).

Scheduleless: the only way in is a manual TaskIQ kick (or a direct call),
and even that no-ops until ``QUOTES_TOSS_CONSUMER_ENABLED=true``.  The
consumer reads the fillwire Redis stream — it never opens a Toss/KIS
websocket and never calls a broker, order, session-kick, or LLM path.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.ohlcv_cache_common import create_redis_client
from app.services.quotes_consumer.consumer import QuotesTossConsumer


async def run_quotes_toss_consumer(
    *,
    once: bool = False,
    stop: Callable[[], bool] | None = None,
) -> dict:
    """Run the resident consumer. Returns the counters summary."""
    if not settings.quotes_toss_consumer_enabled:
        return {"enabled": False, "consumed_entries": 0}
    redis = await create_redis_client()
    try:
        consumer = QuotesTossConsumer(
            redis=redis,
            session_factory=AsyncSessionLocal,
            consumer_name=f"consumer-{socket.gethostname()}",
        )
        counters = await consumer.run(stop=stop, max_cycles=1 if once else None)
        return {"enabled": True, **counters.as_dict()}
    finally:
        await redis.aclose()
