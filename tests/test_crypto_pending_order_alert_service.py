"""ROB-99 crypto pending-order reminder tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.crypto_pending_order_alert_service import (
    CryptoPendingOrderAlertConfig,
    format_pending_order_message,
    normalize_pending_orders,
    run_crypto_pending_order_alert,
)

NOW = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)


def _config() -> CryptoPendingOrderAlertConfig:
    return CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1500719153508515870",
        failure_channel_id="1500722535678083102",
        normal_webhook_url="https://discord.example/normal",
        failure_webhook_url="https://discord.example/failure",
        trader_base_url="https://trader.robinco.dev",
    )


def _order(
    *,
    symbol: str = "KRW-BTC",
    side: str = "sell",
    order_id: str = "12345678-aaaa-bbbb-cccc-123456789abc",
    price: float = 100_000_000,
    remaining: float = 0.1,
    ordered: float = 0.2,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": "pending",
        "ordered_price": price,
        "remaining_qty": remaining,
        "ordered_qty": ordered,
        "ordered_at": "2026-05-03T23:00:00+00:00",
    }


def test_from_settings_requires_dedicated_webhooks():
    class DummySettings:
        crypto_pending_order_alert_enabled = True
        crypto_pending_order_alert_channel_id = "1500719153508515870"
        crypto_pending_order_failure_channel_id = "1500722535678083102"
        crypto_pending_order_alert_webhook_url = None
        crypto_pending_order_failure_webhook_url = None
        discord_webhook_crypto = "https://discord.example/generic-crypto"
        discord_webhook_alerts = "https://discord.example/generic-alerts"
        trader_base_url = ""

    config = CryptoPendingOrderAlertConfig.from_settings(DummySettings())

    assert config.normal_webhook_url is None
    assert config.failure_webhook_url is None


@pytest.mark.asyncio
async def test_no_orders_is_quiet_success():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [], "errors": []}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "success"
    assert result["orders_count"] == 0
    assert result["normal_alert_sent"] is False
    assert sends == []


@pytest.mark.asyncio
async def test_one_sell_order_sends_normal_payload():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order(side="sell")], "errors": []}

    async def prices(symbols: list[str]) -> dict[str, float]:
        assert symbols == ["KRW-BTC"]
        return {"KRW-BTC": 95_000_000}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "success"
    assert result["normal_alert_sent"] is True
    assert len(sends) == 1
    assert sends[0][0] == "https://discord.example/normal"
    assert "KRW-BTC" in sends[0][1]
    assert "SELL" in sends[0][1]
    assert "+5.26%" in sends[0][1]
    assert "보유/취소 추천이 아닙니다" in sends[0][1]


@pytest.mark.asyncio
async def test_one_buy_order_sends_normal_payload():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {
            "success": True,
            "orders": [_order(side="buy", price=90_000_000)],
            "errors": [],
        }

    async def prices(symbols: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 95_000_000}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "success"
    assert "BUY" in sends[0][1]
    assert "-5.26%" in sends[0][1]


@pytest.mark.asyncio
async def test_multiple_orders_are_grouped_in_one_message():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {
            "success": True,
            "orders": [
                _order(
                    symbol="KRW-BTC", order_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                ),
                _order(
                    symbol="KRW-ETH",
                    order_id="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                    price=4_000_000,
                ),
            ],
            "errors": [],
        }

    async def prices(symbols: list[str]) -> dict[str, float]:
        assert symbols == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 95_000_000, "KRW-ETH": 4_100_000}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "success"
    assert result["orders_count"] == 2
    assert len(sends) == 1
    assert "Crypto pending orders: 2 open" in sends[0][1]
    assert "KRW-BTC" in sends[0][1]
    assert "KRW-ETH" in sends[0][1]


@pytest.mark.asyncio
async def test_malformed_order_symbol_routes_to_failure_alert():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order(symbol="")], "errors": []}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "order_validation"
    assert result["failure_alert_sent"] is True
    assert len(sends) == 1
    assert sends[0][0] == "https://discord.example/failure"
    assert "MalformedOrderRows" in sends[0][1]


@pytest.mark.asyncio
async def test_lookup_failure_sends_failure_alert():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        raise RuntimeError("upbit unavailable")

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "lookup"
    assert result["failure_alert_sent"] is True
    assert sends[0][0] == "https://discord.example/failure"
    assert "upbit unavailable" in sends[0][1]


@pytest.mark.asyncio
async def test_partial_lookup_result_sends_failure_alert():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {
            "success": False,
            "orders": [_order()],
            "errors": [{"market": "crypto", "error": "partial"}],
        }

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert result["failure_alert_sent"] is True
    assert "PartialOrderLookup" in sends[0][1]
    assert "partial data: `true`".lower() in sends[0][1].lower()


@pytest.mark.asyncio
async def test_normal_delivery_failure_is_surfaced_and_failure_alert_sent():
    sends: list[tuple[str, str]] = []

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order()], "errors": []}

    async def prices(symbols: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 95_000_000}

    async def sender(webhook: str, content: str) -> bool:
        sends.append((webhook, content))
        return webhook.endswith("failure")

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=_config(),
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "discord_delivery"
    assert result["failure_alert_sent"] is True
    assert [webhook for webhook, _ in sends] == [
        "https://discord.example/normal",
        "https://discord.example/failure",
    ]


def test_formatter_caps_long_message_for_discord_content_limit():
    orders = normalize_pending_orders(
        [
            _order(
                symbol=f"KRW-COIN{idx}",
                order_id=f"{idx:08d}-aaaa-bbbb-cccc-123456789abc",
            )
            for idx in range(40)
        ],
        {f"KRW-COIN{idx}": 95_000_000 for idx in range(40)},
        now=NOW,
    )
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert len(message) <= 1900
    assert "Crypto pending orders: 40 open" in message
    assert "omitted" in message or "truncated" in message


def test_formatter_keeps_order_id_short_and_includes_link():
    orders = normalize_pending_orders([_order()], {"KRW-BTC": 95_000_000}, now=NOW)
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert "12345678" in message
    assert "12345678-aaaa" not in message
    assert (
        "https://trader.robinco.dev/portfolio?market=crypto&symbol=KRW-BTC" in message
    )
