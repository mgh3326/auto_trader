"""Tests for OpenClaw client integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.openclaw_client import OpenClawClient


@pytest.mark.asyncio
async def test_send_fill_notification_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", False)

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="KRW-BTC",
        side="bid",
        filled_price=50000000,
        filled_qty=0.1,
        filled_amount=5000000,
        filled_at="2024-01-01T00:00:00Z",
        account="upbit",
    )

    result = await OpenClawClient().send_fill_notification(order)
    assert result is None


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_success(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="KRW-BTC",
        side="bid",
        filled_price=50000000,
        filled_qty=0.1,
        filled_amount=5000000,
        filled_at="2024-01-01T00:00:00Z",
        account="upbit",
        order_id="order-123",
    )

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_fill_notification(order)

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:fill"
    assert called_json["wakeMode"] == "now"
    assert "🟢 체결 알림" in called_json["message"]
    mock_notifier.notify_openclaw_message.assert_awaited_once_with(
        called_json["message"]
    )


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_retries_on_failure_then_succeeds(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="KRW-BTC",
        side="ask",
        filled_price=50000000,
        filled_qty=0.1,
        filled_amount=5000000,
        filled_at="2024-01-01T00:00:00Z",
        account="upbit",
    )

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_res_success = MagicMock(status_code=200)
    mock_res_success.raise_for_status.return_value = None

    mock_cli.post.side_effect = [
        mock_res_fail,
        mock_res_fail,
        mock_res_fail,
        mock_res_success,
    ]

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_fill_notification(order)

    assert result is not None
    assert mock_cli.post.call_count == 4


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_returns_none_after_all_retries_fail(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="KRW-BTC",
        side="bid",
        filled_price=50000000,
        filled_qty=0.1,
        filled_amount=5000000,
        filled_at="2024-01-01T00:00:00Z",
        account="upbit",
    )

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_fill_notification(order)

    assert result is None
    assert mock_cli.post.call_count == 4


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_forwards_telegram_when_openclaw_fails(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="KRW-BTC",
        side="bid",
        filled_price=50000000,
        filled_qty=0.1,
        filled_amount=5000000,
        filled_at="2024-01-01T00:00:00Z",
        account="upbit",
    )

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_fill_notification(order)

    assert result is None
    assert mock_cli.post.call_count == 4
    mock_notifier.notify_openclaw_message.assert_awaited_once()
    forwarded_message = mock_notifier.notify_openclaw_message.await_args.args[0]
    assert "체결 알림" in forwarded_message


@pytest.mark.asyncio
async def test_send_scan_alert_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", False)

    result = await OpenClawClient().send_scan_alert("scan message")
    assert result is None


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_scan_alert_success(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_scan_alert("scan message")

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:scan"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"].startswith("auto-trader:scan:")
    assert called_json["message"] == "scan message"
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("scan message")


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_success(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_watch_alert("watch message")

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:watch"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"].startswith("auto-trader:watch:")
    assert called_json["message"] == "watch message"
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("watch message")


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_scan_alert_returns_none_after_all_retries_fail(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_scan_alert("scan message")

    assert result is None
    assert mock_cli.post.call_count == 4


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_scan_alert_forwards_telegram_when_openclaw_fails(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_scan_alert("scan message")

    assert result is None
    assert mock_cli.post.call_count == 4
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("scan message")


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_forwards_telegram_when_openclaw_fails(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await OpenClawClient().send_watch_alert("watch message")

    assert result is None
    assert mock_cli.post.call_count == 4
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("watch message")
