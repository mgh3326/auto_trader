"""ROB-1309 checkpoint fix: enrichment_negative_cache.py's two-tier TTL design.

Regression coverage for a real bug found in review: the original single-TTL
design meant `consecutive_failures` could never exceed 1 in practice —
`should_skip` blocked retries for NEGATIVE_CACHE_TTL_SECONDS, but the Redis
key ALSO expired at exactly that same instant, so the very next attempt
(the first one actually allowed to retry) always saw `prior=None` and reset
the count to 1. `_CHRONIC_FAILURE_THRESHOLD = 3` was unreachable in any real
operating condition.

Fixed by splitting "how long do we block a retry" (blocked_until_epoch,
still NEGATIVE_CACHE_TTL_SECONDS) from "how long do we remember the failure
history" (the Redis key's own TTL, now NEGATIVE_CACHE_HISTORY_TTL_SECONDS,
much longer and slides forward on every failure).
"""

from __future__ import annotations

import time

import pytest

from app.services.invest_view_model import enrichment_negative_cache as negcache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.last_ex: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        if ex is not None:
            self.last_ex[key] = ex

    async def delete(self, key: str):
        self.store.pop(key, None)


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_should_skip_blocks_within_ttl_and_allows_after():
    redis = _FakeRedis()
    await negcache.record_failure(
        redis, kind="kr_sector", market="kr", symbol="005930", exc=RuntimeError("x")
    )
    entry = await negcache.should_skip(
        redis, kind="kr_sector", market="kr", symbol="005930"
    )
    assert entry is not None
    assert entry.consecutive_failures == 1

    # Simulate the block window having elapsed (without the Redis key's own
    # longer history TTL having elapsed) by rewriting a backdated entry.
    stored = await negcache.get_entry(
        redis, kind="kr_sector", market="kr", symbol="005930"
    )
    assert stored is not None
    stored.blocked_until_epoch = time.time() - 1
    import json
    from dataclasses import asdict

    await redis.set(
        negcache._key("kr_sector", "kr", "005930"),
        json.dumps(asdict(stored)),
        ex=negcache.NEGATIVE_CACHE_HISTORY_TTL_SECONDS,
    )

    # Block window elapsed -> should_skip now returns None (retry allowed)
    # even though the failure HISTORY (consecutive_failures) is retained.
    assert (
        await negcache.should_skip(
            redis, kind="kr_sector", market="kr", symbol="005930"
        )
        is None
    )
    retained = await negcache.get_entry(
        redis, kind="kr_sector", market="kr", symbol="005930"
    )
    assert retained is not None
    assert retained.consecutive_failures == 1


async def test_chronic_threshold_is_reachable_across_retry_windows():
    """The bug this regresses: 3 failures spread across 3 separate retry
    windows must accumulate to consecutive_failures=3 (chronic), not reset
    to 1 every time a retry is allowed."""
    redis = _FakeRedis()

    for _ in range(3):
        # Simulate "the block window from the prior failure has elapsed"
        # by backdating blocked_until_epoch on the stored entry (if any)
        # before the next record_failure call — this is exactly what real
        # elapsed wall-clock time would do.
        existing = await negcache.get_entry(
            redis, kind="kr_sector", market="kr", symbol="005930"
        )
        if existing is not None:
            import json
            from dataclasses import asdict

            existing.blocked_until_epoch = time.time() - 1
            await redis.set(
                negcache._key("kr_sector", "kr", "005930"),
                json.dumps(asdict(existing)),
                ex=negcache.NEGATIVE_CACHE_HISTORY_TTL_SECONDS,
            )
        entry = await negcache.record_failure(
            redis,
            kind="kr_sector",
            market="kr",
            symbol="005930",
            exc=RuntimeError("404 not found"),
        )

    assert entry.consecutive_failures == 3
    assert entry.is_chronic()


async def test_record_success_clears_history_not_just_block():
    redis = _FakeRedis()
    await negcache.record_failure(
        redis, kind="consensus", market="us", symbol="AAPL", exc=RuntimeError("x")
    )
    entry = await negcache.get_entry(
        redis, kind="consensus", market="us", symbol="AAPL"
    )
    assert entry is not None and entry.consecutive_failures == 1

    await negcache.record_success(redis, kind="consensus", market="us", symbol="AAPL")

    # Recovery is a full reset, not just "unblock" — the next failure starts
    # a fresh count at 1, not 2 (never permanently penalized after recovery).
    entry = await negcache.record_failure(
        redis, kind="consensus", market="us", symbol="AAPL", exc=RuntimeError("x")
    )
    assert entry.consecutive_failures == 1


async def test_classify_error_recognizes_common_failure_shapes():
    assert negcache.classify_error(RuntimeError("404 Not Found")) == "not_found"
    assert negcache.classify_error(TimeoutError("timed out")) == "timeout"
    assert (
        negcache.classify_error(RuntimeError("429 Too Many Requests")) == "rate_limited"
    )
    assert negcache.classify_error(RuntimeError("weird")) == "unknown"
