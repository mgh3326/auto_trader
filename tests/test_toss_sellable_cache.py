from __future__ import annotations

import logging
from decimal import Decimal

import fakeredis.aioredis
import pytest

from app.core.config import Settings
from app.services import toss_sellable_cache as cache_module
from app.services.toss_sellable_cache import (
    TossSellableCache,
    get_shared_sellable_cache,
    reset_shared_sellable_cache,
)

pytestmark = pytest.mark.unit


class TestTossSellableCacheSettings:
    def test_defaults(self):
        s = Settings()
        assert s.toss_sellable_cache_enabled is True
        assert s.toss_sellable_cache_ttl_seconds == 600.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_ENABLED", "false")
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_TTL_SECONDS", "30")
        s = Settings()
        assert s.toss_sellable_cache_enabled is False
        assert s.toss_sellable_cache_ttl_seconds == 30.0


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_shared_sellable_cache()
    yield
    reset_shared_sellable_cache()


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class _FailingPipeline:
    def __init__(self, message: str) -> None:
        self.message = message

    def set(self, *args, **kwargs):
        return self

    def incr(self, *args, **kwargs):
        return self

    def delete(self, *args, **kwargs):
        return self

    async def execute(self):
        raise RuntimeError(self.message)


class _FailingRedis:
    def __init__(self) -> None:
        self.pipeline_calls = 0

    async def mget(self, *keys):
        raise RuntimeError("redis read unavailable")

    def pipeline(self, **kwargs):
        self.pipeline_calls += 1
        message = (
            "redis pipeline unavailable"
            if self.pipeline_calls == 1
            else "redis delete unavailable"
        )
        return _FailingPipeline(message)

    async def delete(self, *keys):
        raise RuntimeError("redis delete unavailable")


@pytest.mark.asyncio
class TestTossSellableCache:
    async def test_get_many_uses_one_mget_and_preserves_order(
        self, redis_client, mocker
    ):
        cache = TossSellableCache(ttl_seconds=600, redis_client=redis_client)
        await cache.put_many({"AAA": Decimal("3"), "BBB": Decimal("5")})
        mget = mocker.spy(redis_client, "mget")

        assert await cache.get_many(["BBB", "MISSING", "AAA"]) == [
            Decimal("5"),
            None,
            Decimal("3"),
        ]
        assert mget.call_count == 1

    async def test_put_many_sets_ttl_and_get_put_port_remains_available(
        self, redis_client
    ):
        cache = TossSellableCache(ttl_seconds=600, redis_client=redis_client)

        assert await cache.get("BRK.B") is None
        await cache.put("BRK.B", Decimal("1.25"))

        assert await cache.get("BRK.B") == Decimal("1.25")
        assert 0 < await redis_client.ttl("toss:sellable:v1:BRK.B") <= 600

    async def test_invalidate_removes_only_target_symbol(self, redis_client):
        cache = TossSellableCache(ttl_seconds=600, redis_client=redis_client)
        await cache.put_many({"AAA": Decimal("3"), "BBB": Decimal("5")})

        await cache.invalidate("aaa")

        assert await cache.get("AAA") is None
        assert await cache.get("BBB") == Decimal("5")

    async def test_invalidation_generation_blocks_late_stale_write(self, redis_client):
        cache = TossSellableCache(ttl_seconds=600, redis_client=redis_client)
        read = await cache.read_many(["AAA", "BBB"])

        # A sell mutation lands while the miss fanout is still in flight.
        await cache.invalidate("AAA")
        await cache.put_many(
            {"AAA": Decimal("10"), "BBB": Decimal("5")},
            expected_generations=read.generations,
        )

        assert await cache.get("AAA") is None
        assert await cache.get("BBB") == Decimal("5")

    async def test_disabled_is_complete_no_op(self, redis_client, mocker):
        cache = TossSellableCache(
            ttl_seconds=600, redis_client=redis_client, enabled=False
        )
        mget = mocker.spy(redis_client, "mget")
        await cache.put("BRK.B", Decimal("1.25"))

        assert await cache.get("BRK.B") is None
        assert mget.call_count == 0
        assert await redis_client.keys("toss:sellable:v1:*") == []

    async def test_backend_failures_are_log_and_continue(self, caplog):
        cache = TossSellableCache(
            ttl_seconds=600,
            redis_client=_FailingRedis(),
        )

        with caplog.at_level(logging.WARNING):
            assert await cache.get_many(["AAA", "BBB"]) == [None, None]
            await cache.put_many({"AAA": Decimal("3")})
            await cache.invalidate("AAA")

        assert "redis read unavailable" in caplog.text
        assert "redis pipeline unavailable" in caplog.text
        assert "redis delete unavailable" in caplog.text


class TestSingleton:
    def test_shared_instance(self):
        assert get_shared_sellable_cache() is get_shared_sellable_cache()

    def test_reset_drops_instance(self):
        first = get_shared_sellable_cache()
        reset_shared_sellable_cache()
        assert get_shared_sellable_cache() is not first

    @pytest.mark.asyncio
    async def test_redis_client_creation_failure_is_fail_open(
        self, monkeypatch, caplog
    ):
        def fail_from_url(*args, **kwargs):
            raise RuntimeError("invalid redis configuration")

        monkeypatch.setattr(cache_module.redis, "from_url", fail_from_url)
        reset_shared_sellable_cache()

        with caplog.at_level(logging.WARNING):
            cache = get_shared_sellable_cache()
            assert await cache.get("AAA") is None

        assert "invalid redis configuration" in caplog.text
