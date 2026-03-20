from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.watch_alerts import WatchAlertService


class _FakeRedisHash:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    async def hexists(self, key: str, field: str) -> bool:
        return field in self._hashes.get(key, {})

    async def hset(self, key: str, field: str, value: str) -> int:
        target = self._hashes.setdefault(key, {})
        is_new = field not in target
        target[field] = value
        return 1 if is_new else 0

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hdel(self, key: str, field: str) -> int:
        target = self._hashes.get(key, {})
        if field in target:
            target.pop(field, None)
            return 1
        return 0


@pytest.mark.asyncio
async def test_add_watch_is_idempotent_and_preserves_created_at() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    first = await service.add_watch("crypto", "btc", "price_below", 90000000)
    second = await service.add_watch("crypto", "BTC", "price_below", 90000000)

    assert first["created"] is True
    assert second["created"] is False
    assert second["already_exists"] is True

    all_rows = await service.list_watches("crypto")
    watch = all_rows["crypto"][0]
    assert watch["symbol"] == "BTC"
    assert watch["condition_type"] == "price_below"
    assert watch["metric"] == "price"
    assert watch["operator"] == "below"


@pytest.mark.asyncio
async def test_rsi_threshold_out_of_range_raises_value_error() -> None:
    service = WatchAlertService()

    with pytest.raises(ValueError, match="RSI threshold"):
        service.validate_watch_inputs(
            market="crypto",
            symbol="BTC",
            condition_type="rsi_below",
            threshold=101,
        )


@pytest.mark.asyncio
async def test_list_watches_skips_malformed_entries() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    fake_redis._hashes["watch:alerts:crypto"] = {
        "BTC:price_below:90000000": '{"created_at":"2026-02-17T13:00:00+09:00"}',
        "BROKEN_FIELD": '{"created_at":"2026-02-17T13:01:00+09:00"}',
        "ETH:price_above:not-a-number": '{"created_at":"2026-02-17T13:02:00+09:00"}',
    }

    rows = await service.list_watches("crypto")
    assert len(rows["crypto"]) == 1
    assert rows["crypto"][0]["symbol"] == "BTC"
