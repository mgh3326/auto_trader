from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.jobs.watch_proximity_monitor import WatchProximityMonitor


class _FakeWatchService:
    def __init__(self, rows_by_market: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_market = rows_by_market
        self.removed: list[tuple[str, str]] = []
        self.closed = False

    async def get_watches_for_market(self, market: str) -> list[dict[str, object]]:
        return list(self.rows_by_market.get(market, []))

    async def trigger_and_remove(self, market: str, field: str) -> bool:
        self.removed.append((market, field))
        return True

    async def close(self) -> None:
        self.closed = True


class _FakeDedupeStore:
    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.ttls: list[int] = []

    async def mark_if_new(self, key: str, ttl_seconds: int) -> bool:
        self.ttls.append(ttl_seconds)
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_market_closed_skips_without_quote_fetch_or_notify() -> None:
    watch_service = _FakeWatchService(
        {
            "kr": [
                {
                    "target_kind": "asset",
                    "symbol": "005930",
                    "condition_type": "price_above",
                    "threshold": 100.0,
                    "field": "asset:005930:price_above:100",
                }
            ]
        }
    )
    current_value_provider = AsyncMock(return_value=99.8)
    notifier = AsyncMock()
    monitor = WatchProximityMonitor(
        watch_service=watch_service,
        current_value_provider=current_value_provider,
        market_open_provider=lambda market: False,
        dedupe_store=_FakeDedupeStore(),
        notifier=notifier,
    )

    result = await monitor.scan_market("kr")

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"
    assert result["market_open"] is False
    assert result["evaluated"] == 0
    assert result["skipped"] == 1
    current_value_provider.assert_not_awaited()
    notifier.assert_not_awaited()
    assert watch_service.removed == []


@pytest.mark.asyncio
async def test_active_price_watch_within_band_sends_one_notification() -> None:
    watch_service = _FakeWatchService(
        {
            "crypto": [
                {
                    "target_kind": "asset",
                    "symbol": "BTC",
                    "condition_type": "price_above",
                    "threshold": 100.0,
                    "field": "asset:BTC:price_above:100",
                }
            ]
        }
    )
    notifier = AsyncMock()
    monitor = WatchProximityMonitor(
        watch_service=watch_service,
        current_value_provider=AsyncMock(return_value=99.6),
        market_open_provider=lambda market: True,
        dedupe_store=_FakeDedupeStore(),
        notifier=notifier,
    )

    result = await monitor.scan_market("crypto", cooldown_seconds=300)

    assert result["status"] == "success"
    assert result["evaluated"] == 1
    assert result["notified"] == 1
    assert result["deduped"] == 0
    notifier.assert_awaited_once()
    message = notifier.await_args.kwargs["message"]
    assert "BTC price_above" in message
    assert "final user approval" in message
    assert watch_service.removed == []


@pytest.mark.asyncio
async def test_second_run_within_cooldown_dedupes_notification() -> None:
    dedupe_store = _FakeDedupeStore()
    notifier = AsyncMock()
    watch_service = _FakeWatchService(
        {
            "crypto": [
                {
                    "target_kind": "asset",
                    "symbol": "ETH",
                    "condition_type": "price_below",
                    "threshold": 100.0,
                    "field": "asset:ETH:price_below:100",
                }
            ]
        }
    )
    monitor = WatchProximityMonitor(
        watch_service=watch_service,
        current_value_provider=AsyncMock(return_value=100.4),
        market_open_provider=lambda market: True,
        dedupe_store=dedupe_store,
        notifier=notifier,
    )

    first = await monitor.scan_market("crypto", cooldown_seconds=600)
    second = await monitor.scan_market("crypto", cooldown_seconds=600)

    assert first["notified"] == 1
    assert second["status"] == "skipped"
    assert second["reason"] == "all_notifications_deduped"
    assert second["deduped"] == 1
    assert notifier.await_count == 1
    assert dedupe_store.ttls == [600, 600]
    assert watch_service.removed == []


@pytest.mark.asyncio
async def test_hit_band_sends_notification_but_does_not_remove_watch() -> None:
    watch_service = _FakeWatchService(
        {
            "crypto": [
                {
                    "target_kind": "asset",
                    "symbol": "SOL",
                    "condition_type": "price_above",
                    "threshold": 100.0,
                    "field": "asset:SOL:price_above:100",
                }
            ]
        }
    )
    notifier = AsyncMock()
    monitor = WatchProximityMonitor(
        watch_service=watch_service,
        current_value_provider=AsyncMock(return_value=101.0),
        market_open_provider=lambda market: True,
        dedupe_store=_FakeDedupeStore(),
        notifier=notifier,
    )

    result = await monitor.scan_market("crypto")

    assert result["notified"] == 1
    assert result["details"][0]["band"] == "hit"
    notifier.assert_awaited_once()
    assert watch_service.removed == []


@pytest.mark.asyncio
async def test_unsupported_non_price_watches_are_skipped_not_failed() -> None:
    monitor = WatchProximityMonitor(
        watch_service=_FakeWatchService(
            {
                "kr": [
                    {
                        "target_kind": "asset",
                        "symbol": "005930",
                        "condition_type": "rsi_below",
                        "threshold": 30.0,
                        "field": "asset:005930:rsi_below:30",
                    },
                    {
                        "target_kind": "asset",
                        "symbol": "000660",
                        "condition_type": "trade_value_above",
                        "threshold": 1_000_000.0,
                        "field": "asset:000660:trade_value_above:1000000",
                    },
                ]
            }
        ),
        current_value_provider=AsyncMock(return_value=29.0),
        market_open_provider=lambda market: True,
        dedupe_store=_FakeDedupeStore(),
        notifier=AsyncMock(),
    )

    result = await monitor.scan_market("kr")

    assert result["status"] == "skipped"
    assert result["reason"] == "no_proximity_alerts"
    assert result["evaluated"] == 0
    assert result["skipped"] == 2
    assert result["unsupported"] == 2


def test_monitor_module_does_not_import_order_or_registration_boundaries() -> None:
    import app.jobs.watch_proximity_monitor as module

    source = inspect.getsource(module)
    forbidden = [
        "app.services.orders",
        "kis_trading_service",
        "order_execution",
        "orders_registration",
        "watch_alerts_registration",
        "create_order_intent",
        "submit_order",
        "place_order",
        "register_watch_alert",
    ]
    for token in forbidden:
        assert token not in source
