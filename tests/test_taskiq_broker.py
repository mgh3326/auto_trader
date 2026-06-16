"""Tests for TaskIQ broker startup middleware."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.core.taskiq_broker as taskiq_broker


def _make_middleware(*, is_worker_process: bool, is_scheduler_process: bool):
    middleware = taskiq_broker.WorkerInitMiddleware()
    middleware.broker = SimpleNamespace(
        is_worker_process=is_worker_process, is_scheduler_process=is_scheduler_process
    )
    return middleware


def _set_notification_settings(
    monkeypatch,
    *,
    telegram_token=None,
    telegram_chat_id=None,
    discord_webhook_us=None,
    discord_webhook_kr=None,
    discord_webhook_crypto=None,
    discord_webhook_alerts=None,
):
    monkeypatch.setattr(taskiq_broker.settings, "telegram_token", telegram_token)
    monkeypatch.setattr(taskiq_broker.settings, "telegram_chat_id", telegram_chat_id)
    monkeypatch.setattr(
        taskiq_broker.settings,
        "discord_webhook_us",
        discord_webhook_us,
    )
    monkeypatch.setattr(
        taskiq_broker.settings,
        "discord_webhook_kr",
        discord_webhook_kr,
    )
    monkeypatch.setattr(
        taskiq_broker.settings,
        "discord_webhook_crypto",
        discord_webhook_crypto,
    )
    monkeypatch.setattr(
        taskiq_broker.settings,
        "discord_webhook_alerts",
        discord_webhook_alerts,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_init_middleware_initializes_sentry_for_scheduler(monkeypatch):
    middleware = _make_middleware(
        is_worker_process=False,
        is_scheduler_process=True,
    )

    mock_init_sentry = Mock()
    monkeypatch.setattr(taskiq_broker, "init_sentry", mock_init_sentry)

    await middleware.startup()

    mock_init_sentry.assert_called_once_with(service_name="auto-trader-scheduler")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_init_middleware_configures_notifier_for_discord_only(monkeypatch):
    middleware = _make_middleware(
        is_worker_process=True,
        is_scheduler_process=False,
    )
    mock_init_sentry = Mock()
    mock_configure = Mock(return_value=True)
    monkeypatch.setattr(taskiq_broker, "init_sentry", mock_init_sentry)
    monkeypatch.setattr(
        taskiq_broker,
        "configure_trade_notifier_from_settings",
        mock_configure,
    )

    discord_settings = {
        "discord_webhook_us": "https://discord.example/us",
        "discord_webhook_kr": "https://discord.example/kr",
        "discord_webhook_crypto": "https://discord.example/crypto",
        "discord_webhook_alerts": "https://discord.example/alerts",
    }
    _set_notification_settings(
        monkeypatch,
        telegram_token=None,
        telegram_chat_id=None,
        **discord_settings,
    )

    await middleware.startup()

    mock_init_sentry.assert_called_once_with(
        service_name="auto-trader-worker",
        enable_sqlalchemy=True,
        enable_httpx=True,
    )
    mock_configure.assert_called_once_with(log_context="Worker trade notifier")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_init_middleware_configures_notifier_for_discord_and_telegram(
    monkeypatch,
):
    middleware = _make_middleware(
        is_worker_process=True,
        is_scheduler_process=False,
    )
    mock_init_sentry = Mock()
    mock_configure = Mock(return_value=True)
    monkeypatch.setattr(taskiq_broker, "init_sentry", mock_init_sentry)
    monkeypatch.setattr(
        taskiq_broker,
        "configure_trade_notifier_from_settings",
        mock_configure,
    )

    discord_settings = {
        "discord_webhook_us": "https://discord.example/us",
        "discord_webhook_kr": "https://discord.example/kr",
        "discord_webhook_crypto": "https://discord.example/crypto",
        "discord_webhook_alerts": "https://discord.example/alerts",
    }
    _set_notification_settings(
        monkeypatch,
        telegram_token="telegram-token",
        telegram_chat_id="123456789",
        **discord_settings,
    )

    await middleware.startup()

    mock_init_sentry.assert_called_once_with(
        service_name="auto-trader-worker",
        enable_sqlalchemy=True,
        enable_httpx=True,
    )
    mock_configure.assert_called_once_with(log_context="Worker trade notifier")
