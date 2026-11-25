import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.token_issuance import KISTokenIssuanceRateLimitedError
from app.services.redis_token_manager import RedisTokenManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_absolute_kst_expiry_wins_when_earlier(monkeypatch):
    import app.services.redis_token_manager as token_manager_module

    now = datetime(2026, 11, 25, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    monkeypatch.setattr(token_manager_module, "time", SimpleNamespace(time=lambda: now))
    manager = RedisTokenManager()
    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(manager, "_get_redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(manager, "get_token", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "_release_lock", AsyncMock())

    async def token_response():
        return "opaque-test-token", 7200, "2026-11-25 10:30:00"

    assert await manager.refresh_token_with_lock(token_response) == "opaque-test-token"

    saved = json.loads(redis.set.await_args.args[1])
    expected = datetime(2026, 11, 25, 10, 30, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    assert saved["expires_at"] == expected
    assert redis.set.await_args.kwargs["ex"] == 1860


@pytest.mark.asyncio
async def test_bad_or_missing_absolute_expiry_uses_expires_in(monkeypatch):
    import app.services.redis_token_manager as token_manager_module

    now = 1_000_000.0
    monkeypatch.setattr(token_manager_module, "time", SimpleNamespace(time=lambda: now))
    manager = RedisTokenManager()
    redis = AsyncMock()
    monkeypatch.setattr(manager, "_get_redis_client", AsyncMock(return_value=redis))

    for absolute_expiry in (None, "bad"):
        await manager.save_token(
            "opaque-test-token", 120, absolute_expiry=absolute_expiry
        )
        saved = json.loads(redis.set.await_args.args[1])
        assert saved["expires_at"] == now + 120


@pytest.mark.asyncio
async def test_egw00133_sets_minute_cooldown_with_fake_clock(monkeypatch):
    import app.services.redis_token_manager as token_manager_module

    class Clock:
        now = 1000.0

    class FakeRedis:
        values: dict[str, tuple[str, float]] = {}

        async def get(self, key):
            value = self.values.get(key)
            if value is None or value[1] <= Clock.now:
                self.values.pop(key, None)
                return None
            return value[0]

        async def set(self, key, value, *, ex=None, **_kwargs):
            self.values[key] = (value, Clock.now + ex)
            return True

    monkeypatch.setattr(
        token_manager_module, "time", SimpleNamespace(time=lambda: Clock.now)
    )
    monkeypatch.setattr(token_manager_module.asyncio, "sleep", AsyncMock())
    manager = RedisTokenManager()
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        manager, "_get_redis_client", AsyncMock(return_value=fake_redis)
    )
    monkeypatch.setattr(manager, "get_token", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "_release_lock", AsyncMock())
    fetcher = AsyncMock(side_effect=KISTokenIssuanceRateLimitedError())

    with pytest.raises(KISTokenIssuanceRateLimitedError) as first:
        await manager.refresh_token_with_lock(fetcher)
    with pytest.raises(KISTokenIssuanceRateLimitedError) as second:
        await manager.refresh_token_with_lock(fetcher)
    assert first.value.code == second.value.code == "EGW00133"
    assert fetcher.await_count == 1

    Clock.now += 60
    fetcher.side_effect = None
    fetcher.return_value = ("next-token", 3600, None)
    monkeypatch.setattr(manager, "save_token", AsyncMock())
    token = await manager.refresh_token_with_lock(fetcher)
    assert token == "next-token"
    assert fetcher.await_count == 2
