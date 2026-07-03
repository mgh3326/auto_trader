from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_view_model import analyst_consensus_cache as cache

pytestmark = pytest.mark.unit

_KST = ZoneInfo("Asia/Seoul")


def test_cache_key_is_kst_dated_and_namespaced():
    now = datetime(2026, 7, 4, 9, 0, tzinfo=_KST)
    assert (
        cache._consensus_cache_key("kr", "005930", now)
        == "screener_consensus:naver:005930:2026-07-04"
    )


def test_strip_volatile_drops_current_price_and_upside():
    consensus = {
        "buy_count": 2,
        "hold_count": 1,
        "sell_count": 0,
        "total_count": 3,
        "avg_target_price": 78500,
        "current_price": 69900,
        "upside_pct": 12.3,
    }
    stable = cache._strip_volatile(consensus)
    assert stable["buy_count"] == 2
    assert stable["avg_target_price"] == 78500
    assert "current_price" not in stable
    assert "upside_pct" not in stable


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.mark.asyncio
async def test_set_then_get_round_trips_stable_fields_only():
    redis = _FakeRedis()
    consensus = {
        "buy_count": 2,
        "total_count": 3,
        "avg_target_price": 78500,
        "current_price": 69900,
        "upside_pct": 12.3,
    }
    await cache.set_cached_consensus(redis, "kr", "005930", consensus)
    got = await cache.get_cached_consensus(redis, "kr", "005930")
    assert got is not None
    assert got["total_count"] == 3
    assert got["avg_target_price"] == 78500
    assert "current_price" not in got  # volatile never persisted
    assert "upside_pct" not in got


@pytest.mark.asyncio
async def test_set_is_noop_for_degraded_or_us_or_no_redis():
    redis = _FakeRedis()
    # total_count 0 → degraded, never cached
    await cache.set_cached_consensus(redis, "kr", "000660", {"total_count": 0})
    assert await cache.get_cached_consensus(redis, "kr", "000660") is None
    # US never cached
    await cache.set_cached_consensus(redis, "us", "AAPL", {"total_count": 5})
    assert redis.store == {}
    # redis None → no-op, no raise
    await cache.set_cached_consensus(None, "kr", "005930", {"total_count": 3})


@pytest.mark.asyncio
async def test_get_fail_open_on_malformed_and_none():
    assert await cache.get_cached_consensus(None, "kr", "005930") is None

    class _Boom:
        async def get(self, key):
            raise RuntimeError("redis down")

    assert await cache.get_cached_consensus(_Boom(), "kr", "005930") is None


@pytest.mark.asyncio
async def test_resolve_consensus_uses_cache_and_skips_live_fetch():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis,
        "kr",
        "005930",
        {"buy_count": 2, "total_count": 3, "avg_target_price": 78500},
    )
    calls: list[str] = []

    async def _live(*, symbol, market, limit):
        calls.append(symbol)
        return {"consensus": {"total_count": 99}}

    got = await cache.resolve_consensus(
        symbol="005930", market="kr", redis_client=redis, opinion_fetcher=_live
    )
    assert got is not None and got["total_count"] == 3  # from cache, not live 99
    assert calls == []  # live fetcher never called


@pytest.mark.asyncio
async def test_resolve_consensus_populates_cache_and_memo_on_miss():
    redis = _FakeRedis()
    memo: dict = {}
    calls: list[str] = []

    async def _live(*, symbol, market, limit):
        calls.append(symbol)
        return {
            "consensus": {
                "buy_count": 1,
                "total_count": 2,
                "avg_target_price": 100,
                "current_price": 90,
                "upside_pct": 11.1,
            }
        }

    got = await cache.resolve_consensus(
        symbol="000660",
        market="kr",
        redis_client=redis,
        memo=memo,
        opinion_fetcher=_live,
    )
    assert got["total_count"] == 2 and "current_price" not in got  # stable only
    assert (await cache.get_cached_consensus(redis, "kr", "000660"))["total_count"] == 2
    # second resolve is served from memo — live fetcher not called again
    await cache.resolve_consensus(
        symbol="000660",
        market="kr",
        redis_client=redis,
        memo=memo,
        opinion_fetcher=_live,
    )
    assert calls == ["000660"]


@pytest.mark.asyncio
async def test_resolve_consensus_counts_maps_symbols():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis, "kr", "005930", {"buy_count": 2, "total_count": 3}
    )

    async def _live(*, symbol, market, limit):
        return {"consensus": {"buy_count": 5, "total_count": 7}}

    memo: dict = {}
    counts = await cache.resolve_consensus_counts(
        symbols=["005930", "000660"],
        market="kr",
        redis_client=redis,
        memo=memo,
        opinion_fetcher=_live,
    )
    assert counts["005930"] == {"totalCount": 3, "buyCount": 2}  # cache
    assert counts["000660"] == {"totalCount": 7, "buyCount": 5}  # live


@pytest.mark.asyncio
async def test_cached_opinion_provider_recomputes_upside_from_fresh_price():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis,
        "kr",
        "005930",
        {
            "buy_count": 2,
            "hold_count": 1,
            "sell_count": 0,
            "total_count": 3,
            "avg_target_price": 110,
        },
    )

    async def _price(code):
        return 100  # fresh price → upside = (110-100)/100*100 = 10.0

    payload = await cache.cached_opinion_provider(
        symbol="005930",
        market="kr",
        redis_client=redis,
        price_fetcher=_price,
    )
    c = payload["consensus"]
    assert c["current_price"] == 100
    assert c["upside_pct"] == pytest.approx(10.0)
    assert c["total_count"] == 3  # daily-stable count preserved


@pytest.mark.asyncio
async def test_cached_opinion_provider_fail_open_when_price_missing():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis,
        "kr",
        "005930",
        {"total_count": 3, "avg_target_price": 110},
    )

    async def _price(code):
        return None  # price unavailable → no stale upside served

    payload = await cache.cached_opinion_provider(
        symbol="005930",
        market="kr",
        redis_client=redis,
        price_fetcher=_price,
    )
    c = payload["consensus"]
    assert c.get("upside_pct") is None
    assert c.get("current_price") is None
