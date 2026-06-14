"""Tests for OpenClaw client integration."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from tenacity import wait_fixed

import app.services.openclaw_client as openclaw_client
from app.core.config import settings
from app.services.openclaw_client import (
    OpenClawClient,
    WatchAlertDeliveryResult,
    _build_openclaw_message,
)


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


def test_build_openclaw_message_can_omit_model_name() -> None:
    message = _build_openclaw_message(
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
def zero_delay_openclaw_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openclaw_client,
        "OPENCLAW_RETRY_WAIT",
        wait_fixed(0),
        raising=False,
    )


# =============================================================================
# N8N Fill Notification Tests removed as part of redesign
# =============================================================================


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
@patch(
    "app.services.openclaw_client.uuid4",
    return_value=UUID("00000000-0000-0000-0000-000000000001"),
)
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_request_analysis_supports_screener_callback_schema(
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

    await OpenClawClient().request_analysis(
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


# Obsolete: OpenClaw thread-based tests removed
# New n8n-based fill notification tests are above


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
async def test_send_watch_alert_to_router_skips_when_no_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WATCH_ALERT_ROUTER_URL", "")
    monkeypatch.setattr(settings, "N8N_WATCH_ALERT_WEBHOOK_URL", "")

    result = await OpenClawClient().send_watch_alert_to_router(
        message="watch message",
        market="crypto",
        triggered=[{"symbol": "BTC", "condition_type": "price_above"}],
        as_of="2026-04-17T00:00:00+09:00",
        correlation_id="corr-watch-skip",
    )

    assert result.status == "skipped"
    assert result.reason == "router_not_configured"
    assert result.request_id is None


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_posts_payload(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WATCH_ALERT_ROUTER_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )
    monkeypatch.setattr(settings, "N8N_WATCH_ALERT_WEBHOOK_URL", "")

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="watch summary",
        market="kr",
        triggered=[
            {
                "symbol": "005930",
                "condition_type": "price_below",
                "threshold": 70000,
                "current": 69000,
            }
        ],
        as_of="2026-04-17T09:30:00+09:00",
        correlation_id="corr-watch-ok",
    )

    assert result.status == "success"
    assert result.reason is None
    assert result.request_id is not None
    called_url = mock_cli.post.call_args.args[0]
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_url == pytest.approx("http://127.0.0.1:5678/webhook/watch-alert")
    assert called_json["alert_type"] == "watch"
    assert called_json["correlation_id"] == "corr-watch-ok"
    assert called_json["as_of"] == "2026-04-17T09:30:00+09:00"
    assert called_json["market"] == "kr"
    assert called_json["triggered"] == [
        {
            "symbol": "005930",
            "condition_type": "price_below",
            "threshold": 70000,
            "current": 69000,
        }
    ]
    assert called_json["message"] == "watch summary"


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_returns_failed_on_retries_exhausted(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WATCH_ALERT_ROUTER_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )
    monkeypatch.setattr(settings, "N8N_WATCH_ALERT_WEBHOOK_URL", "")

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("watch 5xx")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="watch summary",
        market="crypto",
        triggered=[{"symbol": "BTC", "condition_type": "price_above"}],
        as_of="2026-04-17T00:00:00+09:00",
        correlation_id="corr-watch-fail",
    )

    assert result.status == "failed"
    assert result.reason == "request_failed"
    assert result.request_id is None
    assert mock_cli.post.call_count == 4


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_prefers_router_url_over_legacy(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "WATCH_ALERT_ROUTER_URL",
        "http://127.0.0.1:9999/router/watch-alert",
    )
    monkeypatch.setattr(
        settings,
        "N8N_WATCH_ALERT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res
    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="m",
        market="kr",
        triggered=[{"symbol": "X", "condition_type": "price_below"}],
        as_of="2026-04-17T00:00:00Z",
        correlation_id="corr-prefer-router",
    )

    assert result.status == "success"
    assert mock_cli.post.call_args.args[0] == pytest.approx(
        "http://127.0.0.1:9999/router/watch-alert"
    )


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_watch_alert_to_router_falls_back_to_legacy_n8n_url(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WATCH_ALERT_ROUTER_URL", "")
    monkeypatch.setattr(
        settings,
        "N8N_WATCH_ALERT_WEBHOOK_URL",
        "http://127.0.0.1:5678/webhook/watch-alert",
    )

    mock_cli = AsyncMock()
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res
    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    result = await OpenClawClient().send_watch_alert_to_router(
        message="m",
        market="kr",
        triggered=[{"symbol": "X", "condition_type": "price_below"}],
        as_of="2026-04-17T00:00:00Z",
        correlation_id="corr-fallback",
    )

    assert result.status == "success"
    assert (
        mock_cli.post.call_args.args[0] == "http://127.0.0.1:5678/webhook/watch-alert"
    )


def test_watch_alert_delivery_result_enforces_request_id_contract() -> None:
    with pytest.raises(ValueError, match="success results require a request_id"):
        WatchAlertDeliveryResult(status="success")

    with pytest.raises(
        ValueError,
        match="request_id is only allowed for success results",
    ):
        WatchAlertDeliveryResult(status="failed", request_id="req-123")


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_scan_alert_skips_telegram_when_mirror_disabled(
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

    result = await OpenClawClient().send_scan_alert(
        "scan message",
        mirror_to_telegram=False,
    )

    assert result is not None
    mock_cli.post.assert_awaited_once()
    mock_notifier.notify_openclaw_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
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

    start = time.monotonic()
    result = await OpenClawClient().send_scan_alert("scan message")
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
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

    start = time.monotonic()
    result = await OpenClawClient().send_scan_alert("scan message")
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("scan message")


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
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

    start = time.monotonic()
    result = await OpenClawClient().send_watch_alert("watch message")
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    mock_notifier.notify_openclaw_message.assert_awaited_once_with("watch message")
