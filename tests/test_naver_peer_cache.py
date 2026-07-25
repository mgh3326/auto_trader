from __future__ import annotations

import pytest

from app.services.naver_finance import peer_cache

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex))


async def test_set_then_get_integration_roundtrips_with_ttl(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "naver_peer_cache_ttl_seconds", 600)
    r = _FakeRedis()
    payload = {"symbol": "006400", "per": 12.3, "market_cap": 999, "peers_raw": []}
    await peer_cache.set_cached_integration(r, "006400", payload)
    assert r.set_calls and r.set_calls[0][2] == 600  # TTL applied

    got = await peer_cache.get_cached_integration(r, "006400")
    assert got == payload


async def test_get_with_none_client_returns_none_fail_open():
    assert await peer_cache.get_cached_integration(None, "006400") is None
    # set with None client is a no-op (must not raise)
    await peer_cache.set_cached_integration(None, "006400", {"a": 1})


async def test_get_client_returns_none_when_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "naver_peer_cache_enabled", False)
    assert await peer_cache._get_redis_client() is None


async def test_malformed_cache_value_returns_none(monkeypatch):
    r = _FakeRedis()
    r.store["naver_peer:integ:006400"] = "{not json"
    assert await peer_cache.get_cached_integration(r, "006400") is None


async def test_sector_payload_roundtrips(monkeypatch):
    r = _FakeRedis()
    payload = {"sector_name": "반도체", "extra_codes": ["000660", "000990"]}
    await peer_cache.set_cached_sector(r, "123", payload)
    assert await peer_cache.get_cached_sector(r, "123") == payload
