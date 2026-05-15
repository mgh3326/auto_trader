from datetime import UTC, datetime, timedelta

import pytest

from app.services.upbit_public_read_model.orderbook_cache import OrderbookCache
from app.services.upbit_public_read_model.types import ORDERBOOK_TTL_SECONDS


class BrokenRedis:
    async def get(self, key):
        raise RuntimeError("redis get unavailable")

    async def set(self, key, value, ex=None):
        raise RuntimeError("redis set unavailable")


def _book(market="KRW-BTC"):
    return {
        "market": market,
        "orderbook_units": [
            {"ask_price": 100.0, "bid_price": 99.0, "ask_size": 1, "bid_size": 1}
        ],
    }


@pytest.mark.asyncio
async def test_orderbook_cache_computes_spread_and_hits_cache(fake_redis):
    calls = 0

    async def fetcher(markets):
        nonlocal calls
        calls += 1
        return {m: _book(m) for m in markets}

    cache = OrderbookCache(redis=fake_redis, fetcher=fetcher)
    first = await cache.get(["KRW-BTC"])
    second = await cache.get(["KRW-BTC"])
    assert first.spreadsPct["KRW-BTC"] == pytest.approx((100 - 99) / 99 * 100)
    assert second.meta.state == "fresh"
    assert calls == 1


@pytest.mark.asyncio
async def test_orderbook_cache_stale_and_unavailable(fake_redis, monkeypatch):
    async def fetcher_ok(markets):
        return {m: _book(m) for m in markets}

    cache = OrderbookCache(redis=fake_redis, fetcher=fetcher_ok)
    await cache.get(["KRW-BTC"])
    monkeypatch.setattr(
        "app.services.upbit_public_read_model.orderbook_cache._now_utc",
        lambda: datetime.now(UTC) + timedelta(seconds=ORDERBOOK_TTL_SECONDS + 1),
    )

    async def fail(markets):
        raise RuntimeError("boom")

    cache._fetcher = fail
    assert (await cache.get(["KRW-BTC"])).meta.state == "stale"
    empty = OrderbookCache(redis=fake_redis, fetcher=fail)
    await fake_redis.flushall()
    assert (await empty.get(["KRW-ETH"])).meta.state == "unavailable"


@pytest.mark.asyncio
async def test_orderbook_cache_partial_fetch_uses_stale_cache(fake_redis, monkeypatch):
    async def fetcher_ok(markets):
        return {m: _book(m) for m in markets}

    cache = OrderbookCache(redis=fake_redis, fetcher=fetcher_ok)
    await cache.get(["KRW-BTC"])
    monkeypatch.setattr(
        "app.services.upbit_public_read_model.orderbook_cache._now_utc",
        lambda: datetime.now(UTC) + timedelta(seconds=ORDERBOOK_TTL_SECONDS + 1),
    )

    async def partial(markets):
        assert markets == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-ETH": _book("KRW-ETH")}

    cache._fetcher = partial
    block = await cache.get(["KRW-BTC", "KRW-ETH"])
    assert block.meta.state == "stale"
    assert block.meta.errorReason == "partial_missing"
    assert set(block.orderbooks) == {"KRW-BTC", "KRW-ETH"}


@pytest.mark.asyncio
async def test_orderbook_cache_empty_fetch_without_cache_is_unavailable(fake_redis):
    async def empty(markets):
        return {}

    cache = OrderbookCache(redis=fake_redis, fetcher=empty)
    block = await cache.get(["KRW-BTC"])
    assert block.meta.state == "unavailable"
    assert block.meta.errorReason == "partial_missing"
    assert block.orderbooks == {}


@pytest.mark.asyncio
async def test_orderbook_cache_returns_fresh_when_redis_is_unavailable():
    calls = 0

    async def fetcher(markets):
        nonlocal calls
        calls += 1
        return {m: _book(m) for m in markets}

    cache = OrderbookCache(redis=BrokenRedis(), fetcher=fetcher)
    block = await cache.get(["KRW-BTC"])
    assert block.meta.state == "fresh"
    assert block.spreadsPct["KRW-BTC"] == pytest.approx((100 - 99) / 99 * 100)
    assert calls == 1
