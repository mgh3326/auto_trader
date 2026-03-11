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
from app.monitoring.trade_notifier import TradeNotifier, get_trade_notifier
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
        market_type="crypto",
    )

    result = await OpenClawClient().send_fill_notification(order)
    assert result is None


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_success(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        market_type="crypto",
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

    with caplog.at_level("INFO"):
        result = await OpenClawClient().send_fill_notification(
            order, correlation_id="corr-fill-success"
        )

    assert result is not None
    mock_cli.post.assert_awaited_once()
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert called_json["name"] == "auto-trader:fill"
    assert called_json["wakeMode"] == "now"
    assert "🟢 체결 알림" in called_json["message"]
    mock_notifier.notify_openclaw_message.assert_awaited_once_with(
        called_json["message"],
        correlation_id="corr-fill-success",
        market_type="crypto",
    )
    assert "correlation_id=corr-fill-success" in caplog.text
    assert f"request_id={result}" in caplog.text


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_raw_mapping_forwards_canonical_market_type(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    order = {
        "symbol": "AAPL",
        "side": "02",
        "filled_price": 195.5,
        "filled_qty": 2,
        "filled_amount": 391,
        "filled_at": "2026-02-14T09:30:00-05:00",
        "account": "kis",
        "market": "NASDAQ",
    }

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

    result = await OpenClawClient().send_fill_notification(
        order, correlation_id="corr-fill-raw-market"
    )

    assert result is not None
    mock_notifier.notify_openclaw_message.assert_awaited_once_with(
        mock_notifier.notify_openclaw_message.await_args.args[0],
        correlation_id="corr-fill-raw-market",
        market_type="us",
    )


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_partial_event_uses_partial_message_and_order_id_session(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENCLAW_ENABLED", True)
    monkeypatch.setattr(settings, "OPENCLAW_WEBHOOK_URL", "http://openclaw/hooks/agent")
    monkeypatch.setattr(settings, "OPENCLAW_TOKEN", "test-token")

    from app.services.fill_notification import FillOrder

    order = FillOrder(
        symbol="AAPL",
        side="bid",
        filled_price=195.5,
        filled_qty=2,
        filled_amount=391,
        filled_at="2026-02-14T09:30:00-05:00",
        account="kis",
        order_id="us-order-12345",
        fill_status="partial",
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
    called_json = mock_cli.post.call_args.kwargs["json"]
    assert "구분: 매수 부분체결" in called_json["message"]
    assert called_json["sessionKey"] == "auto-trader:fill:kis:us-order-12345"


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_retries_on_failure_then_succeeds(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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

    start = time.monotonic()
    with caplog.at_level("INFO"):
        result = await OpenClawClient().send_fill_notification(
            order, correlation_id="corr-fill-retry"
        )
    elapsed = time.monotonic() - start

    assert result is not None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    assert "correlation_id=corr-fill-retry" in caplog.text
    assert "attempt=1" in caplog.text
    assert "attempt=4" in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_returns_none_after_all_retries_fail(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        market_type="crypto",
    )

    mock_cli = AsyncMock()
    mock_res_fail = MagicMock()
    mock_res_fail.raise_for_status.side_effect = Exception("Network error")
    mock_cli.post.return_value = mock_res_fail

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    start = time.monotonic()
    with caplog.at_level("INFO"):
        result = await OpenClawClient().send_fill_notification(
            order, correlation_id="corr-fill-failed"
        )
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    assert "correlation_id=corr-fill-failed" in caplog.text
    assert "failed after retries" in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("zero_delay_openclaw_retry_wait")
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_forwards_telegram_when_openclaw_fails(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        market_type="crypto",
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

    start = time.monotonic()
    with caplog.at_level("INFO"):
        result = await OpenClawClient().send_fill_notification(
            order, correlation_id="corr-openclaw-fail-mirror-success"
        )
    elapsed = time.monotonic() - start

    assert result is None
    assert mock_cli.post.call_count == 4
    assert elapsed < 2.0
    forwarded_message = mock_notifier.notify_openclaw_message.await_args.args[0]
    mock_notifier.notify_openclaw_message.assert_awaited_once_with(
        forwarded_message,
        correlation_id="corr-openclaw-fail-mirror-success",
        market_type="crypto",
    )
    assert "체결 알림" in forwarded_message
    assert "corr-openclaw-fail-mirror-success" in caplog.text


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_logs_mirror_failure_when_openclaw_succeeds(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    mock_notifier = MagicMock()
    mock_notifier.notify_openclaw_message = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: mock_notifier,
    )

    with caplog.at_level("INFO"):
        result = await OpenClawClient().send_fill_notification(
            order, correlation_id="corr-openclaw-success-mirror-fail"
        )

    assert result is not None
    mock_notifier.notify_openclaw_message.assert_awaited_once()


@pytest.mark.asyncio
@patch("app.services.openclaw_client.httpx.AsyncClient")
async def test_send_fill_notification_disabled_notifier_emits_single_summary_log(
    mock_httpx_client_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    mock_res = MagicMock(status_code=200)
    mock_res.raise_for_status.return_value = None
    mock_cli.post.return_value = mock_res

    mock_client_instance = AsyncMock()
    mock_client_instance.__aenter__.return_value = mock_cli
    mock_client_instance.__aexit__.return_value = None
    mock_httpx_client_cls.return_value = mock_client_instance

    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    notifier = get_trade_notifier()
    notifier.configure(bot_token="test-token", chat_ids=["123456"], enabled=False)
    monkeypatch.setattr(
        "app.services.openclaw_client.get_trade_notifier",
        lambda: notifier,
    )

    try:
        with caplog.at_level("INFO"):
            result = await OpenClawClient().send_fill_notification(
                order, correlation_id="corr-real-disabled"
            )
    finally:
        TradeNotifier._instance = None
        TradeNotifier._initialized = False

    assert result is not None
    summary_lines = [
        record.getMessage()
        for record in caplog.records
        if "OpenClaw mirror result:" in record.getMessage()
    ]
    assert len(summary_lines) == 1
    assert "correlation_id=corr-real-disabled" in summary_lines[0]
    assert "discord=skipped(notifier_disabled)" in summary_lines[0]
    assert "telegram=skipped(notifier_disabled)" in summary_lines[0]


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
