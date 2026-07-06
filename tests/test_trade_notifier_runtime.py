from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.monitoring.trade_notifier import runtime


def _settings(**overrides):
    base = {
        "discord_webhook_us": None,
        "discord_webhook_kr": None,
        "discord_webhook_crypto": None,
        "discord_webhook_alerts": None,
        "telegram_token": None,
        "telegram_chat_id": None,
        "telegram_chat_ids": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_configure_trade_notifier_disabled_without_transports(monkeypatch):
    get_notifier = Mock()
    monkeypatch.setattr(runtime, "get_trade_notifier", get_notifier)

    configured = runtime.configure_trade_notifier_from_settings(
        log_context="Unit notifier",
        settings_obj=_settings(),
    )

    assert configured is False
    get_notifier.assert_not_called()


@pytest.mark.unit
def test_configure_trade_notifier_passes_discord_and_telegram(monkeypatch):
    notifier = SimpleNamespace(configure=Mock())
    monkeypatch.setattr(runtime, "get_trade_notifier", Mock(return_value=notifier))

    configured = runtime.configure_trade_notifier_from_settings(
        log_context="Unit notifier",
        settings_obj=_settings(
            discord_webhook_us="https://discord.example/us",
            discord_webhook_kr="https://discord.example/kr",
            telegram_token="telegram-token",
            telegram_chat_id="primary-chat",
            telegram_chat_ids=["primary-chat", "secondary-chat"],
        ),
    )

    assert configured is True
    notifier.configure.assert_called_once_with(
        bot_token="telegram-token",
        chat_ids=["primary-chat", "secondary-chat"],
        enabled=True,
        discord_webhook_us="https://discord.example/us",
        discord_webhook_kr="https://discord.example/kr",
        discord_webhook_crypto=None,
        discord_webhook_alerts=None,
    )


@pytest.mark.unit
def test_configure_trade_notifier_uses_plural_chat_ids_without_singular_chat_id(
    monkeypatch,
):
    notifier = SimpleNamespace(configure=Mock())
    monkeypatch.setattr(runtime, "get_trade_notifier", Mock(return_value=notifier))

    configured = runtime.configure_trade_notifier_from_settings(
        log_context="Unit notifier",
        settings_obj=_settings(
            telegram_token="telegram-token",
            telegram_chat_id=None,
            telegram_chat_ids=["primary-chat", "secondary-chat"],
        ),
    )

    assert configured is True
    notifier.configure.assert_called_once_with(
        bot_token="telegram-token",
        chat_ids=["primary-chat", "secondary-chat"],
        enabled=True,
        discord_webhook_us=None,
        discord_webhook_kr=None,
        discord_webhook_crypto=None,
        discord_webhook_alerts=None,
    )


@pytest.mark.unit
def test_configure_trade_notifier_fail_open_on_configure_error(monkeypatch):
    notifier = SimpleNamespace(configure=Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(runtime, "get_trade_notifier", Mock(return_value=notifier))

    configured = runtime.configure_trade_notifier_from_settings(
        log_context="Unit notifier",
        settings_obj=_settings(discord_webhook_alerts="https://discord.example/alerts"),
    )

    assert configured is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_trade_notifier_closes_singleton(monkeypatch):
    notifier = SimpleNamespace(shutdown=AsyncMock())
    monkeypatch.setattr(runtime, "get_trade_notifier", Mock(return_value=notifier))

    await runtime.shutdown_trade_notifier(log_context="Unit notifier")

    notifier.shutdown.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_trade_notifier_fail_open_on_shutdown_error(monkeypatch):
    notifier = SimpleNamespace(shutdown=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(runtime, "get_trade_notifier", Mock(return_value=notifier))

    await runtime.shutdown_trade_notifier(log_context="Unit notifier")

    notifier.shutdown.assert_awaited_once()
