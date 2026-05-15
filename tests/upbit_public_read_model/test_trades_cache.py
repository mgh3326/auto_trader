from datetime import UTC, datetime, timedelta

import pytest

from app.services.upbit_public_read_model.trades_cache import TradesCache
from app.services.upbit_public_read_model.types import (
    TRADES_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitTradesBlock,
)


class BrokenRedis:
    async def get(self, key):
        raise RuntimeError("redis get unavailable")

    async def set(self, key, value, ex=None):
        raise RuntimeError("redis set unavailable")


@pytest.mark.asyncio
async def test_trades_cache_fresh_empty_and_bounds_count(fake_redis):
    captured = {}

    async def fetcher(market, count):
        captured["args"] = (market, count)
        return []

    cache = TradesCache(redis=fake_redis, fetcher=fetcher)
    block = await cache.get("KRW-BTC", 9999)
    assert captured["args"] == ("KRW-BTC", 500)
    assert block.meta.state == "fresh"
    assert block.trades == {"KRW-BTC": []}


@pytest.mark.asyncio
async def test_trades_cache_stale_and_merge(fake_redis, monkeypatch):
    async def ok(market, count):
        return [{"market": market, "trade_price": 1.0}]

    cache = TradesCache(redis=fake_redis, fetcher=ok)
    await cache.get("KRW-BTC", 50)
    monkeypatch.setattr(
        "app.services.upbit_public_read_model.trades_cache._now_utc",
        lambda: datetime.now(UTC) + timedelta(seconds=TRADES_TTL_SECONDS + 1),
    )

    async def fail(market, count):
        raise RuntimeError("boom")

    cache._fetcher = fail
    stale = await cache.get("KRW-BTC", 50)
    unavailable = UpbitTradesBlock(
        meta=UpbitBlockMeta(
            source="upbit_trades", state="unavailable", label="Upbit trades"
        )
    )
    merged = TradesCache.merge([stale, unavailable])
    assert stale.meta.state == "stale"
    assert merged.meta.state == "unavailable"
    assert "KRW-BTC" in merged.trades


@pytest.mark.asyncio
async def test_trades_cache_returns_fresh_when_redis_is_unavailable():
    calls = 0

    async def fetcher(market, count):
        nonlocal calls
        calls += 1
        return [{"market": market, "trade_price": 1.0}]

    cache = TradesCache(redis=BrokenRedis(), fetcher=fetcher)
    block = await cache.get("KRW-BTC", 50)
    assert block.meta.state == "fresh"
    assert block.trades["KRW-BTC"][0]["trade_price"] == 1.0
    assert calls == 1
