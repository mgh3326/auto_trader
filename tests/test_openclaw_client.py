"""Tests for OpenClaw client integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.core.config import settings
from app.services.openclaw_client import OpenClawClient, _build_openclaw_message


def test_build_openclaw_message_includes_callback_and_schema() -> None:
    message = _build_openclaw_message(
        request_id="rid-123",
        prompt="PROMPT",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        callback_url="http://example.test/api/v1/openclaw/callback",
        callback_token="cb-token",
    )

    assert "USER_PROMPT:\nPROMPT" in message
    assert "POST http://example.test/api/v1/openclaw/callback" in message
    assert "Authorization: Bearer cb-token" in message
    assert "RESPONSE_JSON_SCHEMA (example):" in message

    schema_json = message.split("RESPONSE_JSON_SCHEMA (example):\n", 1)[1].strip()
    schema = json.loads(schema_json)
    assert schema["request_id"] == "rid-123"
    assert schema["symbol"] == "AAPL"
    assert schema["name"] == "Apple Inc."
    assert schema["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_request_analysis_raises_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", False)

    with pytest.raises(RuntimeError, match="OpenClaw integration is disabled"):
        await OpenClawClient().request_analysis(
            prompt="P",
            symbol="AAPL",
            name="Apple Inc.",
            instrument_type="equity_us",
        )


@pytest.mark.asyncio
@patch(
    "app.services.openclaw_client.uuid4",
    return_value=UUID("00000000-0000-0000-0000-000000000000"),
)
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_request_analysis_posts_payload_and_returns_request_id(
    mock_httpx_client_cls: MagicMock,
    _mock_uuid4: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(
        settings,
        "OPENCLAW_CALLBACK_URL",
        "http://example.test/api/v1/openclaw/callback",
    )
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")
    monkeypatch.setattr(settings, "OPENCLAW_CALLBACK_TOKEN", "cb-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=202)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    request_id = await OpenClawClient().request_analysis(
        prompt="P",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
    )

    assert request_id == "00000000-0000-0000-0000-000000000000"
    mock_httpx_client_cls.assert_called_once_with(timeout=10)

    mock_cli.post.assert_awaited_once()
    called_url = mock_cli.post.call_args.args[0]
    called_json = mock_cli.post.call_args.kwargs["json"]
    called_headers = mock_cli.post.call_args.kwargs["headers"]

    assert called_url == "http://openclaw/hooks/agent"
    assert called_headers["Content-Type"] == "application/json"
    assert called_headers["Authorization"] == "Bearer test-token"

    assert called_json["name"] == "auto-trader:analysis"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"] == f"auto-trader:openclaw:{request_id}"
    assert "USER_PROMPT:\nP" in called_json["message"]
    assert "POST http://example.test/api/v1/openclaw/callback" in called_json["message"]
    assert "Authorization: Bearer cb-token" in called_json["message"]


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

    result = await OpenClawClient().send_fill_notification(order)

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:fill"
    assert "auto-trader:fill:upbit:order-123" in called_json["sessionKey"]
    assert "🟢 체결 알림" in called_json["message"]


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

    result = await OpenClawClient().send_scan_alert("scan message")

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:scan"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"].startswith("auto-trader:scan:")
    assert called_json["message"] == "scan message"


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
