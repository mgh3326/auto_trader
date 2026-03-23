"""
Tests for crypto_trade_cooldown_service module.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis

from app.services import crypto_trade_cooldown_service as cooldown_service


class FakeRedisClient:
    """Fake Redis client for testing."""

    def __init__(self):
        self._data = {}
        self.set = AsyncMock(side_effect=self._set)
        self.get = AsyncMock(side_effect=self._get)
        self.mget = AsyncMock(side_effect=self._mget)
        self.ttl = AsyncMock(side_effect=self._ttl)

    async def _set(self, key: str, value: str, ex: int = None):
        self._data[key] = {"value": value, "ttl": ex}

    async def _get(self, key: str):
        entry = self._data.get(key)
        return entry["value"] if entry else None

    async def _mget(self, keys: list[str]):
        return [self._data.get(key, {}).get("value") for key in keys]

    async def _ttl(self, key: str):
        entry = self._data.get(key)
        return entry.get("ttl", -1) if entry else -1


@pytest.mark.asyncio
async def test_record_stop_loss_sets_ttl(monkeypatch):
    """Test that record_stop_loss stores a TTL-backed key for 8 days."""
    fake_redis = FakeRedisClient()
    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    await service.record_stop_loss("KRW-BTC")

    fake_redis.set.assert_awaited_once_with(
        "crypto:stop_loss_cooldown:KRW-BTC",
        "1",
        ex=8 * 24 * 60 * 60,
    )


@pytest.mark.asyncio
async def test_is_in_cooldown_returns_true_when_key_exists(monkeypatch):
    """Test that is_in_cooldown returns True when Redis has the key."""
    fake_redis = FakeRedisClient()
    await fake_redis._set("crypto:stop_loss_cooldown:KRW-ETH", "1", ex=1000)

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    result = await service.is_in_cooldown("KRW-ETH")

    assert result is True


@pytest.mark.asyncio
async def test_is_in_cooldown_returns_false_when_key_absent(monkeypatch):
    """Test that is_in_cooldown returns False when key is absent."""
    fake_redis = FakeRedisClient()

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    result = await service.is_in_cooldown("KRW-XRP")

    assert result is False


@pytest.mark.asyncio
async def test_is_in_cooldown_degrades_safely_on_read_failure(
    monkeypatch, caplog
):
    """Test that is_in_cooldown returns False and logs warning on Redis failure."""
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(side_effect=Exception("Redis connection failed"))

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    with caplog.at_level(logging.WARNING):
        result = await service.is_in_cooldown("KRW-BTC")

    assert result is False
    assert "crypto stop-loss cooldown read failed" in caplog.text


@pytest.mark.asyncio
async def test_record_stop_loss_degrades_safely_on_write_failure(
    monkeypatch, caplog
):
    """Test that record_stop_loss does not raise on Redis failure."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(side_effect=Exception("Redis connection failed"))

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    with caplog.at_level(logging.WARNING):
        await service.record_stop_loss("KRW-BTC")  # should not raise

    assert "crypto stop-loss cooldown write failed" in caplog.text


@pytest.mark.asyncio
async def test_get_remaining_ttl_seconds_returns_ttl(monkeypatch):
    """Test that get_remaining_ttl_seconds returns the TTL value."""
    fake_redis = FakeRedisClient()
    await fake_redis._set("crypto:stop_loss_cooldown:KRW-BTC", "1", ex=86400)

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    result = await service.get_remaining_ttl_seconds("KRW-BTC")

    assert result == 86400


@pytest.mark.asyncio
async def test_get_remaining_ttl_seconds_returns_none_when_key_absent(monkeypatch):
    """Test that get_remaining_ttl_seconds returns None when key is absent."""
    fake_redis = FakeRedisClient()

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    result = await service.get_remaining_ttl_seconds("KRW-XRP")

    assert result is None


@pytest.mark.asyncio
async def test_symbol_normalization_to_uppercase(monkeypatch):
    """Test that symbols are normalized to uppercase."""
    fake_redis = FakeRedisClient()

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    await service.record_stop_loss("krw-btc")

    fake_redis.set.assert_awaited_once_with(
        "crypto:stop_loss_cooldown:KRW-BTC",
        "1",
        ex=8 * 24 * 60 * 60,
    )


@pytest.mark.asyncio
async def test_filter_symbols_in_cooldown_returns_blocked_subset(monkeypatch):
    """Batch lookup returns only symbols currently blocked by cooldown."""
    fake_redis = FakeRedisClient()
    await fake_redis._set("crypto:stop_loss_cooldown:KRW-BTC", "1", ex=100)
    await fake_redis._set("crypto:stop_loss_cooldown:KRW-XRP", "1", ex=100)

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    blocked = await service.filter_symbols_in_cooldown(
        ["krw-btc", "KRW-ETH", "KRW-XRP"]
    )

    assert blocked == {"KRW-BTC", "KRW-XRP"}


@pytest.mark.asyncio
async def test_filter_symbols_in_cooldown_degrades_safely(monkeypatch, caplog):
    """Batch lookup returns empty set when Redis read fails."""
    fake_redis = MagicMock()
    fake_redis.mget = AsyncMock(side_effect=Exception("Redis connection failed"))

    monkeypatch.setattr(
        redis,
        "from_url",
        AsyncMock(return_value=fake_redis),
    )

    service = cooldown_service.CryptoTradeCooldownService()
    with caplog.at_level(logging.WARNING):
        blocked = await service.filter_symbols_in_cooldown(["KRW-BTC", "KRW-ETH"])

    assert blocked == set()
    assert "crypto stop-loss cooldown batch read failed" in caplog.text
