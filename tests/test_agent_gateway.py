"""Tests for the agent gateway client integration (formerly OpenClaw)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from tenacity import wait_fixed

import app.services.agent_gateway as agent_gateway
from app.core.config import settings
from app.services.agent_gateway import (
    AgentGatewayClient,
    _build_agent_message,
)


def test_build_agent_message_includes_callback_and_schema() -> None:
    message = _build_agent_message(
        request_id="rid-123",
        prompt="PROMPT",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        callback_url="http://example.test/api/v1/agent/callback",
        callback_token="cb-token",
    )

    assert "USER_PROMPT:\nPROMPT" in message
    assert "POST http://example.test/api/v1/agent/callback" in message
    assert "Authorization: Bearer cb-token" in message
    assert "RESPONSE_JSON_SCHEMA (example):" in message

    schema_json = message.split("RESPONSE_JSON_SCHEMA (example):\n", 1)[1].strip()
    schema = json.loads(schema_json)
    assert schema["request_id"] == "rid-123"
    assert schema["symbol"] == "AAPL"
    assert schema["name"] == "Apple Inc."
    assert schema["instrument_type"] == "equity_us"


def test_build_agent_message_can_omit_model_name() -> None:
    message = _build_agent_message(
        request_id="rid-456",
        prompt="PROMPT",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        callback_url="http://example.test/api/screener/callback",
        callback_token="cb-token",
        include_model_name=False,
    )

    schema_json = message.split("RESPONSE_JSON_SCHEMA (example):\n", 1)[1].strip()
    schema = json.loads(schema_json)
    assert "model_name" not in schema


@pytest.fixture
def zero_delay_agent_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_gateway,
        "AGENT_GATEWAY_RETRY_WAIT",
        wait_fixed(0),
        raising=False,
    )


@pytest.mark.asyncio
async def test_request_analysis_raises_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", False)

    with pytest.raises(RuntimeError, match="Agent gateway integration is disabled"):
        await AgentGatewayClient().request_analysis(
            prompt="P",
            symbol="AAPL",
            name="Apple Inc.",
            instrument_type="equity_us",
        )


@pytest.mark.asyncio
@patch(
    "app.services.agent_gateway.uuid4",
    return_value=UUID("00000000-0000-0000-0000-000000000000"),
)
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_request_analysis_posts_payload_and_returns_request_id(
    mock_httpx_client_cls: MagicMock,
    _mock_uuid4: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(
        settings,
        "AGENT_GATEWAY_CALLBACK_URL",
        "http://example.test/api/v1/agent/callback",
    )
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_CALLBACK_TOKEN", "cb-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=202)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    request_id = await AgentGatewayClient().request_analysis(
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

    assert called_url == "http://agent/hooks/agent"
    assert called_headers["Content-Type"] == "application/json"
    assert called_headers["Authorization"] == "Bearer test-token"

    assert called_json["name"] == "auto-trader:analysis"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"] == f"auto-trader:agent:{request_id}"
    assert "USER_PROMPT:\nP" in called_json["message"]
    assert "POST http://example.test/api/v1/agent/callback" in called_json["message"]
    assert "Authorization: Bearer cb-token" in called_json["message"]


@pytest.mark.asyncio
@patch(
    "app.services.agent_gateway.uuid4",
    return_value=UUID("00000000-0000-0000-0000-000000000001"),
)
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_request_analysis_supports_screener_callback_schema(
    mock_httpx_client_cls: MagicMock,
    _mock_uuid4: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(
        settings,
        "AGENT_GATEWAY_CALLBACK_URL",
        "http://example.test/api/v1/agent/callback",
    )
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_CALLBACK_TOKEN", "cb-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=202)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    await AgentGatewayClient().request_analysis(
        prompt="P",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        callback_url="http://example.test/api/screener/callback",
        include_model_name=False,
    )

    called_json = mock_cli.post.call_args.kwargs["json"]
    assert "POST http://example.test/api/screener/callback" in called_json["message"]
    schema_json = (
        called_json["message"].split("RESPONSE_JSON_SCHEMA (example):\n", 1)[1].strip()
    )
    schema = json.loads(schema_json)
    assert "model_name" not in schema


@pytest.mark.asyncio
async def test_send_scan_alert_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", False)

    result = await AgentGatewayClient().send_scan_alert("scan message")
    assert result is None


@pytest.mark.asyncio
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_send_scan_alert_success(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_agent_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.agent_gateway.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await AgentGatewayClient().send_scan_alert("scan message")

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:scan"
    assert called_json["wakeMode"] == "now"
    assert called_json["sessionKey"].startswith("auto-trader:scan:")
    assert called_json["message"] == "scan message"
    mock_notifier.notify_agent_message.assert_awaited_once_with("scan message")


@pytest.mark.asyncio
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_send_scan_alert_skips_telegram_when_mirror_disabled(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_agent_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.agent_gateway.get_trade_notifier",
        lambda: mock_notifier,
    )

    result = await AgentGatewayClient().send_scan_alert(
        "scan message",
        mirror_to_telegram=False,
    )

    assert result is not None
    mock_cli.post.assert_awaited_once()
    mock_notifier.notify_agent_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_agent_retry_wait")
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_send_scan_alert_returns_none_after_all_retries_fail(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    start = time.monotonic()
    result = await AgentGatewayClient().send_scan_alert("scan message")
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_agent_retry_wait")
@patch("app.services.agent_gateway.httpx.AsyncClient")
async def test_send_scan_alert_forwards_telegram_when_agent_fails(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "AGENT_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_GATEWAY_URL", "http://agent/hooks/agent")
    monkeypatch.setattr(settings, "AGENT_GATEWAY_TOKEN", "test-token")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_agent_message = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.agent_gateway.get_trade_notifier",
        lambda: mock_notifier,
    )

    start = time.monotonic()
    result = await AgentGatewayClient().send_scan_alert("scan message")
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    mock_notifier.notify_agent_message.assert_awaited_once_with("scan message")
