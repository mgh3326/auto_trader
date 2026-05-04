from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.jobs import crypto_pending_order_alert as alert


@pytest.mark.asyncio
async def test_no_pending_orders_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return []

    async def fail_send(**_: Any) -> bool:  # pragma: no cover - should not be called
        raise AssertionError("no-orders run must not send Discord messages")

    monkeypatch.setattr(alert, "fetch_open_orders", fake_fetch_open_orders)
    monkeypatch.setattr(alert, "send_discord_channel_message", fail_send)

    result = await alert.run_crypto_pending_order_alert(execute=True, enabled=True)

    assert result["success"] is True
    assert result["status"] == "no_orders"
    assert result["sent"] is False
    assert result["orders_count"] == 0


@pytest.mark.asyncio
async def test_dry_run_formats_safe_pending_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "uuid": "12345678-90ab-cdef-1234-567890abcdef",
                "market": "KRW-BTC",
                "side": "ask",
                "state": "wait",
                "price": "120000000",
                "remaining_volume": "0.01",
                "executed_volume": "0.002",
                "created_at": "2026-05-04T01:00:00+00:00",
            }
        ]

    async def fake_prices(
        symbols: list[str], use_cache: bool = True
    ) -> dict[str, float]:
        assert symbols == ["KRW-BTC"]
        assert use_cache is False
        return {"KRW-BTC": 117_000_000.0}

    monkeypatch.setattr(alert, "fetch_open_orders", fake_fetch_open_orders)
    monkeypatch.setattr(alert, "fetch_multiple_current_prices", fake_prices)

    result = await alert.run_crypto_pending_order_alert(
        execute=False,
        alert_channel_id="normal-channel",
    )

    assert result["success"] is True
    assert result["status"] == "dry_run_orders_found"
    assert result["sent"] is False
    assert result["orders_count"] == 1
    assert result["target_channel_id"] == "normal-channel"
    preview = result["message_preview"]
    assert "KRW-BTC 매도" in preview
    assert "12345678..." in preview
    assert "90ab-cdef" not in preview
    assert "read-only" in preview
    assert result["orders"][0]["remaining_value_krw"] == "1200000.00"


@pytest.mark.asyncio
async def test_execute_sends_normal_channel_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = alert.PendingCryptoOrder(
        exchange="Upbit",
        symbol="KRW-BTC",
        side="매수",
        order_id_prefix="abcdef12...",
        ordered_price=Decimal("100000000"),
        current_price=Decimal("99000000"),
        distance_pct=Decimal("1.0101"),
        remaining_qty=Decimal("0.01"),
        original_qty=Decimal("0.01"),
        remaining_value_krw=Decimal("1000000"),
        ordered_at="2026-05-04T00:00:00+00:00",
        age_minutes=5,
        status="wait",
        detail_url="https://example.test/KRW-BTC",
    )
    sent_calls: list[dict[str, Any]] = []

    async def fake_collect(**_: Any) -> list[alert.PendingCryptoOrder]:
        return [order]

    async def fake_send(**kwargs: Any) -> bool:
        sent_calls.append(kwargs)
        return True

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fake_collect)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        alert_channel_id="normal-channel",
        failure_channel_id="failure-channel",
    )

    assert result["success"] is True
    assert result["status"] == "orders_found"
    assert result["sent"] is True
    assert [call["channel_id"] for call in sent_calls] == ["normal-channel"]
    assert sent_calls[0]["bot_token"] == "x"


@pytest.mark.asyncio
async def test_collection_failure_routes_to_failure_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_calls: list[dict[str, Any]] = []

    async def fake_collect(**_: Any) -> list[alert.PendingCryptoOrder]:
        raise RuntimeError("upbit unavailable")

    async def fake_send(**kwargs: Any) -> bool:
        sent_calls.append(kwargs)
        return True

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fake_collect)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        alert_channel_id="normal-channel",
        failure_channel_id="failure-channel",
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["failure_sent"] is True
    assert [call["channel_id"] for call in sent_calls] == ["failure-channel"]
    assert "upbit unavailable" in sent_calls[0]["content"]


@pytest.mark.asyncio
async def test_normal_notification_failure_routes_to_failure_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = alert.PendingCryptoOrder(
        exchange="Upbit",
        symbol="KRW-BTC",
        side="매도",
        order_id_prefix="abcdef12...",
        ordered_price=Decimal("120000000"),
        current_price=Decimal("117000000"),
        distance_pct=Decimal("2.56"),
        remaining_qty=Decimal("0.01"),
        original_qty=Decimal("0.01"),
        remaining_value_krw=Decimal("1200000"),
        ordered_at="2026-05-04T00:00:00+00:00",
        age_minutes=5,
        status="wait",
        detail_url="https://example.test/KRW-BTC",
    )
    sent_channels: list[str] = []

    async def fake_collect(**_: Any) -> list[alert.PendingCryptoOrder]:
        return [order]

    async def fake_send(**kwargs: Any) -> bool:
        sent_channels.append(kwargs["channel_id"])
        if kwargs["channel_id"] == "normal-channel":
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("POST", "https://discord.test"),
                response=httpx.Response(status_code=403),
            )
        return True

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fake_collect)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        alert_channel_id="normal-channel",
        failure_channel_id="failure-channel",
    )

    assert result["success"] is False
    assert result["status"] == "notification_failed"
    assert result["failure_sent"] is True
    assert sent_channels == ["normal-channel", "failure-channel"]


@pytest.mark.asyncio
async def test_execute_disabled_skips_before_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_collect(
        **_: Any,
    ) -> list[alert.PendingCryptoOrder]:  # pragma: no cover
        raise AssertionError("disabled execution should not query broker")

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fail_collect)

    result = await alert.run_crypto_pending_order_alert(execute=True, enabled=False)

    assert result["success"] is True
    assert result["status"] == "disabled"
    assert result["sent"] is False


@pytest.mark.asyncio
async def test_normal_notification_false_routes_to_failure_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = alert.PendingCryptoOrder(
        exchange="Upbit",
        symbol="KRW-BTC",
        side="매도",
        order_id_prefix="abcdef12...",
        ordered_price=Decimal("120000000"),
        current_price=Decimal("117000000"),
        distance_pct=Decimal("2.56"),
        remaining_qty=Decimal("0.01"),
        original_qty=Decimal("0.01"),
        remaining_value_krw=Decimal("1200000"),
        ordered_at="2026-05-04T00:00:00+00:00",
        age_minutes=5,
        status="wait",
        detail_url="https://example.test/KRW-BTC",
    )
    sent_channels: list[str] = []

    async def fake_collect(**_: Any) -> list[alert.PendingCryptoOrder]:
        return [order]

    async def fake_send(**kwargs: Any) -> bool:
        sent_channels.append(kwargs["channel_id"])
        return kwargs["channel_id"] != "normal-channel"

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fake_collect)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        alert_channel_id="normal-channel",
        failure_channel_id="failure-channel",
    )

    assert result["success"] is False
    assert result["status"] == "notification_failed"
    assert result["failure_sent"] is True
    assert sent_channels == ["normal-channel", "failure-channel"]
    assert "not delivered" in result["error"]


@pytest.mark.asyncio
async def test_invalid_timezone_falls_back_and_does_not_block_failure_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_calls: list[dict[str, Any]] = []

    async def fake_collect(**_: Any) -> list[alert.PendingCryptoOrder]:
        raise RuntimeError("upbit unavailable")

    async def fake_send(**kwargs: Any) -> bool:
        sent_calls.append(kwargs)
        return True

    monkeypatch.setattr(alert, "collect_pending_crypto_orders", fake_collect)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        failure_channel_id="failure-channel",
        timezone_name="Invalid/Timezone",
    )

    assert result["success"] is False
    assert result["failure_sent"] is True
    assert sent_calls[0]["channel_id"] == "failure-channel"
    assert "KST" in sent_calls[0]["content"]


@pytest.mark.asyncio
async def test_partial_price_lookup_failure_routes_to_failure_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_calls: list[dict[str, Any]] = []

    async def fake_fetch_open_orders(market: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "uuid": "12345678-90ab-cdef-1234-567890abcdef",
                "market": "KRW-BTC",
                "side": "ask",
                "state": "wait",
                "price": "120000000",
                "remaining_volume": "0.01",
                "executed_volume": "0",
                "created_at": "2026-05-04T01:00:00+00:00",
            }
        ]

    async def fake_prices(
        symbols: list[str], use_cache: bool = True
    ) -> dict[str, float]:
        assert symbols == ["KRW-BTC"]
        assert use_cache is False
        return {}

    async def fake_send(**kwargs: Any) -> bool:
        sent_calls.append(kwargs)
        return True

    monkeypatch.setattr(alert, "fetch_open_orders", fake_fetch_open_orders)
    monkeypatch.setattr(alert, "fetch_multiple_current_prices", fake_prices)
    monkeypatch.setattr(alert, "send_discord_channel_message", fake_send)

    result = await alert.run_crypto_pending_order_alert(
        execute=True,
        enabled=True,
        bot_token="x",
        failure_channel_id="failure-channel",
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["failure_sent"] is True
    assert "partial price lookup failure" in result["error"]
    assert [call["channel_id"] for call in sent_calls] == ["failure-channel"]


def test_schedule_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    def identity_decorator(**_: Any) -> Any:
        def decorate(func: Any) -> Any:
            return func

        return decorate

    fake_prefect = SimpleNamespace(
        flow=identity_decorator,
        task=identity_decorator,
        get_run_logger=lambda: SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setitem(sys.modules, "prefect", fake_prefect)

    from scripts import run_crypto_pending_order_alert as script

    monkeypatch.setattr(
        script.settings,
        "CRYPTO_PENDING_ORDER_SCHEDULES",
        "08:30,22:00",
    )

    assert script._schedule_crons() == ["30 8 * * *", "0 22 * * *"]
