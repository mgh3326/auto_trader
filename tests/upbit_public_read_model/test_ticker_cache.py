from datetime import UTC, datetime, timedelta

import pytest

from app.services.upbit_public_read_model.ticker_cache import TickerCache
from app.services.upbit_public_read_model.types import TICKER_TTL_SECONDS


class BrokenRedis:
    async def get(self, key):
        raise RuntimeError("redis get unavailable")

    async def set(self, key, value, ex=None):
        raise RuntimeError("redis set unavailable")


@pytest.mark.asyncio
async def test_ticker_cache_fresh_and_hits_redis(fake_redis):
    calls = 0

    async def fetcher(markets):
        nonlocal calls
        calls += 1
        return [{"market": m, "trade_price": 1.0} for m in markets]

    cache = TickerCache(redis=fake_redis, fetcher=fetcher)
    first = await cache.get(["KRW-BTC"])
    second = await cache.get(["KRW-BTC"])
    assert first.meta.state == second.meta.state == "fresh"
    assert calls == 1
    assert second.meta.cachedAt is not None


@pytest.mark.asyncio
async def test_ticker_cache_returns_stale_on_fetch_failure(fake_redis, monkeypatch):
    async def fetcher_ok(markets):
        return [{"market": m, "trade_price": 1.0} for m in markets]

    cache = TickerCache(redis=fake_redis, fetcher=fetcher_ok)
    await cache.get(["KRW-BTC"])
    monkeypatch.setattr(
        "app.services.upbit_public_read_model.ticker_cache._now_utc",
        lambda: datetime.now(UTC) + timedelta(seconds=TICKER_TTL_SECONDS + 1),
    )

    async def fetcher_fail(markets):
        raise RuntimeError("boom")

    cache._fetcher = fetcher_fail
    block = await cache.get(["KRW-BTC"])
    assert block.meta.state == "stale"
    assert block.tickers["KRW-BTC"]["trade_price"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_ticker_cache_unavailable_and_missing(fake_redis):
    async def fetcher_fail(markets):
        raise RuntimeError("network")

    cache = TickerCache(redis=fake_redis, fetcher=fetcher_fail)
    assert (await cache.get([])).meta.state == "missing"
    block = await cache.get(["KRW-BTC"])
    assert block.meta.state == "unavailable"
    assert block.meta.errorReason == "unknown"


@pytest.mark.asyncio
async def test_ticker_cache_returns_fresh_when_redis_is_unavailable():
    calls = 0

    async def fetcher(markets):
        nonlocal calls
        calls += 1
        return [{"market": m, "trade_price": 1.0} for m in markets]

    cache = TickerCache(redis=BrokenRedis(), fetcher=fetcher)
    block = await cache.get(["KRW-BTC"])
    assert block.meta.state == "fresh"
    assert block.tickers["KRW-BTC"]["trade_price"] == pytest.approx(1.0)
    assert calls == 1
