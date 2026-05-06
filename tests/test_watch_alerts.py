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
    assert watch["target_kind"] == "asset"


@pytest.mark.asyncio
async def test_add_watch_stores_target_kind_in_field_identity() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    result = await service.add_watch(
        "kr",
        "KOSPI",
        "price_below",
        6176.75,
        target_kind="index",
    )

    assert result["target_kind"] == "index"
    assert result["field"] == pytest.approx("index:KOSPI:price_below:6176.75")

    rows = await service.list_watches("kr")
    assert rows["kr"][0]["target_kind"] == "index"
    assert rows["kr"][0]["symbol"] == "KOSPI"
    assert rows["kr"][0]["field"] == pytest.approx("index:KOSPI:price_below:6176.75")


@pytest.mark.asyncio
async def test_legacy_three_part_fields_list_as_asset_and_remove_by_fallback() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]
    fake_redis._hashes["watch:alerts:crypto"] = {
        "BTC:price_below:90000000": '{"created_at":"2026-02-17T13:00:00+09:00"}'
    }

    rows = await service.list_watches("crypto")
    assert rows["crypto"][0]["target_kind"] == "asset"
    assert rows["crypto"][0]["field"] == "BTC:price_below:90000000"

    result = await service.remove_watch(
        "crypto",
        "BTC",
        "price_below",
        90000000,
    )

    assert result["removed"] is True
    assert await fake_redis.hgetall("watch:alerts:crypto") == {}


@pytest.mark.asyncio
async def test_add_watch_treats_legacy_asset_field_as_existing() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]
    fake_redis._hashes["watch:alerts:crypto"] = {
        "BTC:price_below:90000000": '{"created_at":"2026-02-17T13:00:00+09:00"}'
    }

    result = await service.add_watch("crypto", "BTC", "price_below", 90000000)

    assert result["created"] is False
    assert result["already_exists"] is True
    assert result["field"] == "BTC:price_below:90000000"
    assert set((await fake_redis.hgetall("watch:alerts:crypto")).keys()) == {
        "BTC:price_below:90000000"
    }


@pytest.mark.asyncio
async def test_validation_matrix_allows_new_mvp_targets() -> None:
    service = WatchAlertService()

    kr_trade_value = service.validate_watch_inputs(
        market="kr",
        symbol="005930",
        condition_type="trade_value_above",
        threshold=1_000_000_000,
        target_kind="asset",
    )
    index_price = service.validate_watch_inputs(
        market="kr",
        symbol="kosdaq",
        condition_type="price_below",
        threshold=1161.0,
        target_kind="index",
    )
    fx_price = service.validate_watch_inputs(
        market="kr",
        symbol="usdkrw",
        condition_type="price_above",
        threshold=1478,
        target_kind="fx",
    )

    assert kr_trade_value.target_kind == "asset"
    assert kr_trade_value.condition_type == "trade_value_above"
    assert index_price.target_kind == "index"
    assert index_price.symbol == "KOSDAQ"
    assert fx_price.target_kind == "fx"
    assert fx_price.symbol == "USDKRW"


@pytest.mark.asyncio
async def test_validation_matrix_rejects_unsupported_target_metric_pairs() -> None:
    service = WatchAlertService()

    with pytest.raises(ValueError, match="trade_value"):
        service.validate_watch_inputs(
            market="us",
            symbol="AAPL",
            condition_type="trade_value_above",
            threshold=1_000_000,
            target_kind="asset",
        )

    with pytest.raises(ValueError, match="index"):
        service.validate_watch_inputs(
            market="kr",
            symbol="KOSPI",
            condition_type="rsi_below",
            threshold=30,
            target_kind="index",
        )

    with pytest.raises(ValueError, match="USDKRW"):
        service.validate_watch_inputs(
            market="kr",
            symbol="EURKRW",
            condition_type="price_above",
            threshold=1478,
            target_kind="fx",
        )

    with pytest.raises(ValueError, match="market=kr"):
        service.validate_watch_inputs(
            market="us",
            symbol="USDKRW",
            condition_type="price_above",
            threshold=1478,
            target_kind="fx",
        )


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


@pytest.mark.asyncio
async def test_add_watch_intent_payload_round_trips() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    result = await service.add_watch(
        market="kr",
        symbol="005930",
        condition_type="price_below",
        threshold=70000,
        action="create_order_intent",
        side="buy",
        quantity=1,
        max_notional_krw=1500000,
    )
    assert result["created"] is True

    rows = await service.list_watches("kr")
    watch = rows["kr"][0]
    assert watch["action"] == "create_order_intent"
    assert watch["side"] == "buy"
    assert watch["quantity"] == 1
    assert watch["notional_krw"] is None
    assert watch["max_notional_krw"] == 1500000


@pytest.mark.asyncio
async def test_add_watch_rejects_create_order_intent_for_crypto() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    with pytest.raises(ValueError) as excinfo:
        await service.add_watch(
            market="crypto",
            symbol="BTC",
            condition_type="price_below",
            threshold=90000000,
            action="create_order_intent",
            side="buy",
            quantity=1,
        )
    assert "intent_market_unsupported" in str(excinfo.value)


@pytest.mark.asyncio
async def test_legacy_payload_lists_as_notify_only() -> None:
    service = WatchAlertService()
    fake_redis = _FakeRedisHash()
    service._get_redis = AsyncMock(return_value=fake_redis)  # type: ignore[method-assign]

    # Simulate a row written by the pre-ROB-103 code path.
    field = "asset:005930:price_below:70000"
    fake_redis._hashes["watch:alerts:kr"] = {
        field: '{"created_at": "2026-05-04T00:00:00+09:00"}',
    }

    rows = await service.list_watches("kr")
    watch = rows["kr"][0]
    assert watch["action"] == "notify_only"
    assert "side" in watch and watch["side"] is None
