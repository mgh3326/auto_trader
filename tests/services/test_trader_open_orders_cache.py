"""Task 889 — /trader open-orders snapshot cache: TTL, gen bust, last-ok."""

from __future__ import annotations

import datetime as dt

import pytest

from app.schemas.open_orders import (
    OpenOrderRow,
    OpenOrderSourceState,
    OpenOrdersResponse,
)
from app.services.trader_page import open_orders_cache as cache_mod
from app.services.trader_page.open_orders_cache import (
    GEN_KEY,
    TraderOpenOrdersCache,
)

T0 = dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.UTC)  # 12:00 KST


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.down = False

    async def get(self, key: str):
        if self.down:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def incr(self, key: str):
        if self.down:
            raise RuntimeError("redis down")
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value


def _make_cache(fake_redis: _FakeRedis | None = None) -> TraderOpenOrdersCache:
    redis = fake_redis if fake_redis is not None else _FakeRedis()

    async def factory():
        return redis

    return TraderOpenOrdersCache(redis_factory=factory)


def _response(
    *,
    sources: list[OpenOrderSourceState] | None = None,
    items: list[OpenOrderRow] | None = None,
) -> OpenOrdersResponse:
    sources = (
        sources
        if sources is not None
        else [
            OpenOrderSourceState(
                broker="kis",
                market="kr",
                status="ok",
                fetched_at=T0,
                count=0,
            )
        ]
    )
    return OpenOrdersResponse(
        market="all",
        count=len(items or []),
        data_state="ok",
        as_of=T0,
        items=items or [],
        sources=sources,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_hits_within_ttl() -> None:
    cache = _make_cache()
    resp = _response()
    seen = await cache.generation()
    await cache.store(resp, T0, 45, seen_gen=seen)

    hit, cached_at = await cache.read(T0 + dt.timedelta(seconds=30))

    assert hit is resp
    assert cached_at == T0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_misses_after_ttl() -> None:
    cache = _make_cache()
    await cache.store(_response(), T0, 45, seen_gen=await cache.generation())

    hit, _ = await cache.read(T0 + dt.timedelta(seconds=45))

    assert hit is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_bump_invalidates_snapshot() -> None:
    redis = _FakeRedis()
    cache = _make_cache(redis)
    await cache.store(_response(), T0, 45, seen_gen=await cache.generation())
    await redis.incr(GEN_KEY)

    hit, _ = await cache.read(T0 + dt.timedelta(seconds=5))

    assert hit is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_outage_falls_back_to_ttl_only() -> None:
    redis = _FakeRedis()
    cache = _make_cache(redis)
    await cache.store(_response(), T0, 45, seen_gen=await cache.generation())

    redis.down = True
    hit, _ = await cache.read(T0 + dt.timedelta(seconds=10))
    assert hit is not None

    hit, _ = await cache.read(T0 + dt.timedelta(seconds=60))
    assert hit is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_mid_fetch_is_not_absorbed_by_store() -> None:
    """The real race: gen read before the fetch, fill lands mid-fetch."""
    redis = _FakeRedis()
    cache = _make_cache(redis)

    # Service order: capture gen, then fetch broker orders, then store.
    seen = await cache.generation()  # 0 — pinned before the fetch starts
    await redis.incr(GEN_KEY)  # a fill commits while the fetch is in flight
    await cache.store(_response(), T0, 45, seen_gen=seen)

    # The snapshot was fetched pre-fill: the next read MUST miss, not serve
    # it for the TTL with the bumped generation absorbed.
    assert (await cache.read(T0 + dt.timedelta(seconds=1)))[0] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_with_current_gen_hits_until_bump() -> None:
    """No mid-fetch fill: pinned gen stays current until the next bump."""
    redis = _FakeRedis()
    cache = _make_cache(redis)
    await redis.incr(GEN_KEY)  # gen 1 exists before the snapshot is stored

    seen = await cache.generation()
    await cache.store(_response(), T0, 45, seen_gen=seen)

    assert (await cache.read(T0 + dt.timedelta(seconds=1)))[0] is not None

    await redis.incr(GEN_KEY)  # a fill lands -> gen 2
    assert (await cache.read(T0 + dt.timedelta(seconds=2)))[0] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalidate_local_drops_snapshot_but_keeps_last_ok() -> None:
    cache = _make_cache()
    resp = _response(
        sources=[
            OpenOrderSourceState(
                broker="kis", market="kr", status="ok", fetched_at=T0, count=1
            )
        ]
    )
    cache.record_last_ok(resp)
    await cache.store(resp, T0, 45, seen_gen=await cache.generation())

    cache.invalidate_local()

    assert (await cache.read(T0))[0] is None
    assert cache.last_ok("kis", "kr") == T0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_last_ok_ignores_failed_sources() -> None:
    cache = _make_cache()
    resp = _response(
        sources=[
            OpenOrderSourceState(
                broker="toss",
                market="kr",
                status="unavailable",
                fetched_at=T0,
                count=0,
                message="boom",
            )
        ]
    )
    cache.record_last_ok(resp)
    assert cache.last_ok("toss", "kr") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_module_invalidate_clears_and_bumps(monkeypatch) -> None:
    redis = _FakeRedis()
    cache = _make_cache(redis)
    await cache.store(_response(), T0, 45, seen_gen=await cache.generation())
    monkeypatch.setattr(cache_mod, "OPEN_ORDERS_CACHE", cache)

    await cache_mod.invalidate_open_orders_cache()

    assert (await cache.read(T0))[0] is None
    assert redis.store.get(GEN_KEY) == "1"
