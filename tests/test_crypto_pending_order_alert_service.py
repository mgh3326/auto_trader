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
# hk 2603: only flagged symbols (age >= 3d or |gap| >= 10%) get their own line.
AGED = "2026-04-30T00:00:00+00:00"  # 4 days before NOW -> flagged
FRESH = "2026-05-03T23:00:00+00:00"  # 1 hour before NOW


def _config() -> CryptoPendingOrderAlertConfig:
    return CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1500719153508515870",
        failure_channel_id="1500722535678083102",
        normal_webhook_url="https://discord.example/normal",
        failure_webhook_url="https://discord.example/failure",
        discord_bot_token=None,
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
    ordered_at: str = FRESH,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "status": "pending",
        "ordered_price": price,
        "remaining_qty": remaining,
        "ordered_qty": ordered,
        "ordered_at": ordered_at,
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
        return {
            "success": True,
            "orders": [_order(side="sell", ordered_at=AGED)],
            "errors": [],
        }

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
            "orders": [_order(side="buy", price=90_000_000, ordered_at=AGED)],
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
                    symbol="KRW-BTC",
                    order_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    ordered_at=AGED,
                ),
                _order(
                    symbol="KRW-ETH",
                    order_id="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                    price=4_000_000,
                    ordered_at=AGED,
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


@pytest.mark.asyncio
async def test_bot_token_fallback_targets_configured_channel_ids():
    sends: list[tuple[str, str]] = []
    config = CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1500719153508515870",
        failure_channel_id="1500722535678083102",
        normal_webhook_url=None,
        failure_webhook_url=None,
        discord_bot_token="x",
        trader_base_url="https://trader.robinco.dev",
    )

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order(ordered_at=AGED)], "errors": []}

    async def prices(symbols: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 95_000_000}

    async def sender(target: str, content: str) -> bool:
        sends.append((target, content))
        return True

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=config,
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "success"
    assert sends[0][0] == "discord-bot://1500719153508515870/x"
    assert "KRW-BTC" in sends[0][1]


@pytest.mark.asyncio
async def test_missing_all_discord_targets_routes_to_failure_without_delivery():
    config = CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1500719153508515870",
        failure_channel_id="1500722535678083102",
        normal_webhook_url=None,
        failure_webhook_url=None,
        discord_bot_token=None,
        trader_base_url="https://trader.robinco.dev",
    )

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order()], "errors": []}

    async def prices(symbols: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 95_000_000}

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=config,
        order_lookup=lookup,
        price_lookup=prices,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "discord_delivery"
    assert result["failure_alert_sent"] is False
    assert (
        "missing failure Discord webhook URL or bot token"
        in result["failure_delivery_error"]
    )


@pytest.mark.asyncio
async def test_normal_delivery_failure_uses_bot_token_failure_channel():
    sends: list[tuple[str, str]] = []
    config = CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1500719153508515870",
        failure_channel_id="1500722535678083102",
        normal_webhook_url=None,
        failure_webhook_url=None,
        discord_bot_token="x",
        trader_base_url="https://trader.robinco.dev",
    )

    async def lookup() -> dict[str, Any]:
        return {"success": True, "orders": [_order()], "errors": []}

    async def prices(symbols: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 95_000_000}

    async def sender(target: str, content: str) -> bool:
        sends.append((target, content))
        return target.startswith("discord-bot://1500722535678082")

    result = await run_crypto_pending_order_alert(
        execute=True,
        config=config,
        order_lookup=lookup,
        price_lookup=prices,
        discord_sender=sender,
        now=NOW,
    )

    assert result["status"] == "failed"
    assert [target for target, _ in sends] == [
        "discord-bot://1500719153508515870/x",
        "discord-bot://1500722535678083102/x",
    ]


def test_formatter_caps_long_message_for_discord_content_limit():
    orders = normalize_pending_orders(
        [
            _order(
                symbol=f"KRW-COIN{idx}",
                order_id=f"{idx:08d}-aaaa-bbbb-cccc-123456789abc",
                ordered_at=AGED,
            )
            for idx in range(40)
        ],
        {f"KRW-COIN{idx}": 95_000_000 for idx in range(40)},
        now=NOW,
    )
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert len(message) <= 1900
    assert "Crypto pending orders: 40 open" in message
    assert "flagged line(s) cut to fit Discord content limits" in message


def test_formatter_never_prints_full_order_id_and_includes_link():
    orders = normalize_pending_orders(
        [_order(ordered_at=AGED)], {"KRW-BTC": 95_000_000}, now=NOW
    )
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert "12345678-aaaa" not in message
    assert "https://trader.robinco.dev/invest/stocks/crypto/KRW-BTC" in message


@pytest.mark.unit
def test_position_url_uses_invest_stocks():
    from app.services.crypto_pending_order_alert_service import _position_url

    cfg = CryptoPendingOrderAlertConfig(
        enabled=True,
        normal_channel_id="1",
        failure_channel_id="2",
        normal_webhook_url="http://x",
        failure_webhook_url="http://y",
        discord_bot_token=None,
        trader_base_url="https://x.test",
    )
    url = _position_url(cfg, "KRW-BTC")
    assert url == "https://x.test/invest/stocks/crypto/KRW-BTC"
    assert "/portfolio" not in url


def _gap_price(current: float, gap_pct: float) -> float:
    return current * (1 + gap_pct / 100)


@pytest.mark.unit
def test_twenty_one_orders_collapse_to_flagged_symbol_lines():
    # hk 2603: 21 resting rungs across 6 symbols; one symbol is stale (>=3d),
    # one sits >=10% from the price. Only those two get a line.
    prices = {
        "KRW-BTC": 100_000_000,
        "KRW-ETH": 4_000_000,
        "KRW-XRP": 1_000,
        "KRW-SOL": 200_000,
        "KRW-DOGE": 300,
        "KRW-ADA": 1_200,
    }
    raw = []
    counts = {"KRW-BTC": 5, "KRW-ETH": 4, "KRW-XRP": 4, "KRW-SOL": 3, "KRW-DOGE": 3}
    counts["KRW-ADA"] = 2
    for symbol, n in counts.items():
        for i in range(n):
            gap = 2.0 + i
            ordered_at = FRESH
            if symbol == "KRW-ETH" and i == 0:
                ordered_at = AGED
            if symbol == "KRW-SOL" and i == 2:
                gap = 12.5
            raw.append(
                _order(
                    symbol=symbol,
                    order_id=f"{symbol}-{i}",
                    price=_gap_price(prices[symbol], gap),
                    ordered_at=ordered_at,
                )
            )
    assert len(raw) == 21

    orders = normalize_pending_orders(raw, prices, now=NOW)
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)
    lines = message.splitlines()

    assert "Crypto pending orders: 21 open" in lines[0]
    symbol_lines = [line for line in lines if line[:3] in ("1. ", "2. ", "3. ")]
    assert len(symbol_lines) == 2
    assert "`KRW-SOL`" in symbol_lines[0]  # >=10% gap sorts first
    assert "gap≥10%" in symbol_lines[0]
    assert "`KRW-ETH`" in symbol_lines[1]
    assert "age≥3d" in symbol_lines[1]
    assert "Other 4 symbol(s) · 14 order(s)" in message
    for unflagged in ("KRW-BTC", "KRW-XRP", "KRW-DOGE", "KRW-ADA"):
        assert unflagged not in message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ordered_at", "gap_pct", "flagged"),
    [
        ("2026-05-02T00:00:00+00:00", 9.9, False),  # 2 days, 9.9%
        ("2026-05-01T00:00:00+00:00", 9.9, True),  # exactly 3 days
        ("2026-05-02T00:00:00+00:00", 10.0, True),  # exactly 10.0%
        ("2026-05-02T00:00:00+00:00", -10.0, True),  # below the price counts too
    ],
)
def test_flag_boundaries_are_inclusive(ordered_at, gap_pct, flagged):
    current = 1_000_000
    orders = normalize_pending_orders(
        [_order(price=_gap_price(current, gap_pct), ordered_at=ordered_at)],
        {"KRW-BTC": current},
        now=NOW,
    )
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert ("`KRW-BTC`" in message) is flagged
    assert ("Other 1 symbol(s) · 1 order(s)" in message) is (not flagged)


@pytest.mark.unit
def test_symbol_line_aggregates_rungs():
    orders = normalize_pending_orders(
        [
            _order(price=105_000_000, remaining=0.1, ordered_at=AGED),
            _order(price=101_000_000, remaining=0.2),
        ],
        {"KRW-BTC": 100_000_000},
        now=NOW,
    )
    message = format_pending_order_message(orders, config=_config(), run_ts=NOW)

    assert "`KRW-BTC` SELL ×2" in message
    assert "nearest +1.00%" in message
    assert "remaining ₩30,700,000" in message
    assert "oldest 4d 0h" in message
