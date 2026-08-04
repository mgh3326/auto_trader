from __future__ import annotations

import pytest

from app.services.brokers.toss import health


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.increments: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.increments[key] = self.increments.get(key, 0) + 1
        return self.increments[key]

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        assert ex == health._ERROR_TTL_SECONDS
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_published_toss_error_is_redacted_and_readable(monkeypatch) -> None:
    redis = _FakeRedis()

    async def get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(health, "_get_redis_client", get_redis)

    await health.publish_toss_api_error(
        status_code=429,
        error_type="http_response",
        error_code="too-many-requests",
    )
    signal = await health.read_toss_api_error_signal()

    assert signal is not None
    assert signal.sequence == 1
    assert signal.status_code == 429
    assert signal.error_code == "too-many-requests"
    assert "Authorization" not in redis.values[health._ERROR_EVENT_KEY]
    assert "token" not in redis.values[health._ERROR_EVENT_KEY]
