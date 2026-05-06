"""Tests for shared Sentry initialization helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from sentry_sdk.types import Event, Log

import app.monitoring.sentry as sentry_module


@pytest.fixture(autouse=True)
def reset_sentry_state(monkeypatch):
    monkeypatch.setattr(sentry_module, "_initialized", False)
    monkeypatch.setattr(
        sentry_module,
        "_enabled_integration_flags",
        {
            "fastapi": False,
            "sqlalchemy": False,
            "httpx": False,
            "mcp": False,
        },
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_DSN", "")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENVIRONMENT", None)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_PROFILES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_SEND_DEFAULT_PII", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", False)
    monkeypatch.setattr(sentry_module.settings, "ENVIRONMENT", "development")


@pytest.mark.unit
def test_init_sentry_no_dsn(monkeypatch):
    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is False
    mock_init.assert_not_called()


@pytest.mark.unit
def test_init_sentry_uses_build_vcs_ref_release_with_fastapi(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    expected_release = "a" * 40

    def fake_read_text(self: Path, *, encoding: str) -> str:
        assert self == Path("/app/.build-vcs-ref")
        assert encoding == "utf-8"
        return f"  {expected_release}\n"

    mock_init = Mock()
    mock_set_tag = Mock()
    mock_run = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_fastapi=True,
    )

    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://public@example.ingest.sentry.io/1"
    assert kwargs["environment"] == "development"
    assert kwargs["release"] == expected_release
    assert kwargs["traces_sample_rate"] == pytest.approx(1.0)
    assert kwargs["profiles_sample_rate"] == pytest.approx(1.0)
    assert kwargs["send_default_pii"] is True
    assert kwargs["before_send_transaction"] is sentry_module._before_send_transaction
    mock_run.assert_not_called()

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "FastApiIntegration" in integration_names

    mock_set_tag.assert_any_call("service", "auto-trader-api")
    mock_set_tag.assert_any_call("runtime", "python")
    mock_set_tag.assert_any_call("app", "auto-trader")


@pytest.mark.unit
def test_init_sentry_uses_git_sha_when_build_vcs_ref_missing(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    expected_release = "b" * 40

    def fake_read_text(self: Path, *, encoding: str) -> str:
        assert self == Path("/app/.build-vcs-ref")
        assert encoding == "utf-8"
        raise FileNotFoundError

    mock_init = Mock()
    mock_run = Mock(
        return_value=subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=0,
            stdout=f"{expected_release}\n",
        )
    )
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", Mock())
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is True
    assert mock_init.call_args.kwargs["release"] == expected_release
    mock_run.assert_called_once_with(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_init_sentry_uses_git_sha_when_build_vcs_ref_blank(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    expected_release = "c" * 40

    mock_init = Mock()
    mock_run = Mock(
        return_value=subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=0,
            stdout=f"{expected_release}\n",
        )
    )
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", Mock())
    monkeypatch.setattr(Path, "read_text", lambda self, *, encoding: " \n ")
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is True
    assert mock_init.call_args.kwargs["release"] == expected_release
    mock_run.assert_called_once()


@pytest.mark.unit
def test_init_sentry_uses_none_release_when_build_vcs_ref_and_git_fail(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    mock_run = Mock(
        side_effect=subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", "rev-parse", "HEAD"],
        )
    )
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", Mock())
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *, encoding: (_ for _ in ()).throw(OSError("no file")),
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is True
    assert mock_init.call_args.kwargs["release"] is None


@pytest.mark.unit
def test_init_sentry_with_sqlalchemy_and_httpx(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    mock_set_tag = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_sqlalchemy=True,
        enable_httpx=True,
    )

    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "SqlalchemyIntegration" in integration_names
    assert "HttpxIntegration" in integration_names
    assert "FastApiIntegration" not in integration_names


@pytest.mark.unit
def test_init_sentry_all_integrations(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    mock_set_tag = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_fastapi=True,
        enable_sqlalchemy=True,
        enable_httpx=True,
    )

    assert result is True
    kwargs = mock_init.call_args.kwargs

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "FastApiIntegration" in integration_names
    assert "SqlalchemyIntegration" in integration_names
    assert "HttpxIntegration" in integration_names


@pytest.mark.unit
def test_init_sentry_disables_error_log_events(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", False)

    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is True
    kwargs = mock_init.call_args.kwargs
    logging_integration = next(
        integration
        for integration in kwargs["integrations"]
        if type(integration).__name__ == "LoggingIntegration"
    )
    assert logging_integration._handler is None


@pytest.mark.unit
def test_init_sentry_includes_mcp_integration(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", True)

    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    class DummyMCPIntegration:
        def __init__(self, include_prompts: bool = False):
            self.include_prompts = include_prompts

    monkeypatch.setattr(sentry_module, "MCPIntegration", DummyMCPIntegration)

    result = sentry_module.init_sentry("auto-trader-mcp", enable_mcp=True)

    assert result is True
    kwargs = mock_init.call_args.kwargs
    mcp_integration = next(
        integration
        for integration in kwargs["integrations"]
        if type(integration).__name__ == "DummyMCPIntegration"
    )
    assert mcp_integration.include_prompts is True


@pytest.mark.unit
def test_init_sentry_mcp_unavailable(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module, "MCPIntegration", None)

    result = sentry_module.init_sentry("auto-trader-mcp", enable_mcp=True)

    assert result is True
    kwargs = mock_init.call_args.kwargs
    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "MCPIntegration" not in integration_names


@pytest.mark.unit
def test_init_sentry_reinitializes_to_add_mcp(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", True)

    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", Mock())

    class DummyMCPIntegration:
        def __init__(self, include_prompts: bool = False):
            self.include_prompts = include_prompts

    monkeypatch.setattr(sentry_module, "MCPIntegration", DummyMCPIntegration)

    first = sentry_module.init_sentry(
        "auto-trader-api",
        enable_sqlalchemy=True,
        enable_httpx=True,
    )
    second = sentry_module.init_sentry(
        "auto-trader-mcp",
        enable_mcp=True,
    )

    assert first is True
    assert second is True
    assert mock_init.call_count == 2

    first_kwargs = mock_init.call_args_list[0].kwargs
    second_kwargs = mock_init.call_args_list[1].kwargs
    first_names = {
        type(integration).__name__ for integration in first_kwargs["integrations"]
    }
    second_names = {
        type(integration).__name__ for integration in second_kwargs["integrations"]
    }

    assert "DummyMCPIntegration" not in first_names
    assert "DummyMCPIntegration" in second_names
    assert "SqlalchemyIntegration" in second_names
    assert "HttpxIntegration" in second_names


@pytest.mark.unit
def test_before_send_masks_sensitive_fields():
    password_key = "".join(["pass", "word"])
    event: Event = {
        "request": {"headers": {"authorization": "Bearer token", "x-api-key": "abc"}},
        "extra": {"token": "my-token", "nested": {password_key: "secret"}},
    }

    sanitized = sentry_module._before_send(event, {})

    assert sanitized is not None
    request = sanitized.get("request")
    assert isinstance(request, dict)
    headers = request.get("headers")
    assert isinstance(headers, dict)
    assert headers["authorization"] == "[Filtered]"
    assert headers["x-api-key"] == "[Filtered]"

    extra = sanitized.get("extra")
    assert isinstance(extra, dict)
    assert extra["token"] == "[Filtered]"
    nested = extra.get("nested")
    assert isinstance(nested, dict)
    assert nested[password_key] == "[Filtered]"


@pytest.mark.unit
def test_before_send_drops_healthz_uvicorn_access_log():
    event: Event = {
        "logger": "uvicorn.access",
        "logentry": {
            "formatted": '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
        },
    }

    dropped = sentry_module._before_send(event, {})

    assert dropped is None


@pytest.mark.unit
def test_before_send_log_drops_healthz_uvicorn_access_log():
    sentry_log: Log = {
        "severity_text": "error",
        "severity_number": 17,
        "body": '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
        "attributes": {"logger.name": "uvicorn.access"},
        "time_unix_nano": 1,
        "trace_id": None,
        "span_id": None,
    }

    dropped = sentry_module._before_send_log(sentry_log, {})

    assert dropped is None


@pytest.mark.unit
def test_before_send_log_keeps_non_healthz_uvicorn_access_log():
    sentry_log: Log = {
        "severity_text": "error",
        "severity_number": 17,
        "body": '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 200',
        "attributes": {"logger.name": "uvicorn.access"},
        "time_unix_nano": 1,
        "trace_id": None,
        "span_id": None,
    }

    kept = sentry_module._before_send_log(sentry_log, {})

    assert kept is not None
    assert kept.get("body") == pytest.approx(
        '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 200'
    )


@pytest.mark.unit
def test_before_send_transaction_renames_mcp_tool_call():
    event: Event = {
        "transaction": "POST http://127.0.0.1:8765/mcp",
        "spans": [
            {
                "op": "mcp.server",
                "data": {
                    "mcp.tool.name": "get_support_resistance",
                    "mcp.method.name": "tools/call",
                },
            }
        ],
    }

    renamed = sentry_module._before_send_transaction(event, {})

    assert renamed is not None
    assert renamed.get("transaction") == "tools/call get_support_resistance"
    transaction_info = renamed.get("transaction_info")
    assert isinstance(transaction_info, dict)
    assert transaction_info["source"] == "custom"


@pytest.mark.unit
def test_before_send_transaction_drops_protocol_noise_without_mcp_span():
    event: Event = {
        "transaction": "POST http://127.0.0.1:8765/mcp",
        "spans": [
            {
                "op": "http.client",
                "data": {"http.method": "POST"},
            }
        ],
    }

    dropped = sentry_module._before_send_transaction(event, {})

    assert dropped is None


@pytest.mark.unit
def test_before_send_transaction_keeps_non_mcp_transactions():
    event: Event = {
        "transaction": "GET /api/v1/orders",
        "spans": [],
    }

    kept = sentry_module._before_send_transaction(event, {})

    assert kept is event
    assert kept is not None
    assert kept.get("transaction") == "GET /api/v1/orders"


@pytest.mark.unit
def test_capture_exception_adds_masked_context(monkeypatch):
    scope_mock = Mock()
    context_manager = Mock()
    context_manager.__enter__ = Mock(return_value=scope_mock)
    context_manager.__exit__ = Mock(return_value=False)
    new_scope_mock = Mock(return_value=context_manager)

    monkeypatch.setattr(sentry_module, "_initialized", True)
    monkeypatch.setattr(sentry_module.sentry_sdk, "new_scope", new_scope_mock)
    mock_capture = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "capture_exception", mock_capture)

    exc = RuntimeError("boom")
    sentry_module.capture_exception(exc, token="abc", normal_key="value")

    new_scope_mock.assert_called_once_with()
    scope_mock.set_extra.assert_any_call("token", "[Filtered]")
    scope_mock.set_extra.assert_any_call("normal_key", "value")
    mock_capture.assert_called_once_with(exc)


@pytest.mark.unit
class TestYfinanceNoiseFilter:
    def test_yfinance_possibly_delisted_event_dropped(self):
        event: Event = {
            "logger": "yfinance",
            "logentry": {
                "message": (
                    "$DIREXION TESLA 2X: possibly delisted; no price data found "
                    ' (period=5d) (Yahoo error = "No data found, symbol may be '
                    'delisted")'
                ),
                "formatted": (
                    "$DIREXION TESLA 2X: possibly delisted; no price data found "
                    ' (period=5d) (Yahoo error = "No data found, symbol may be '
                    'delisted")'
                ),
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_yfinance_no_data_found_event_dropped(self):
        event: Event = {
            "logger": "yfinance",
            "logentry": {
                "message": "TSLL: No data found for this date range, symbol may be delisted",
                "formatted": "TSLL: No data found for this date range, symbol may be delisted",
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_yfinance_no_price_data_event_dropped(self):
        event: Event = {
            "logger": "yfinance",
            "logentry": {
                "message": "AAPL: possibly delisted; no price data found (period=1y)",
                "formatted": "AAPL: possibly delisted; no price data found (period=1y)",
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_yfinance_real_error_not_dropped(self):
        event: Event = {
            "logger": "yfinance",
            "logentry": {
                "message": "Connection timeout to Yahoo Finance API",
                "formatted": "Connection timeout to Yahoo Finance API",
            },
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_non_yfinance_error_not_dropped(self):
        event: Event = {
            "logger": "app.services",
            "logentry": {
                "message": "Database connection failed",
                "formatted": "Database connection failed",
            },
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_healthcheck_still_filtered(self):
        event: Event = {
            "logger": "uvicorn.access",
            "logentry": {
                "formatted": '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_before_send_drops_yfinance_quote_not_found(self):
        """yfinance 'Quote not found' errors are expected noise when
        symbols are not on Yahoo Finance."""
        event: Event = {
            "logger": "yfinance",
            "message": (
                'HTTP Error 404: {"quoteSummary":{"result":null,"error":'
                '{"code":"Not Found","description":"Quote not found for symbol: A196170"}}}'
            ),
        }
        assert sentry_module._before_send(event, {}) is None

    def test_before_send_log_drops_yfinance_noise(self):
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": (
                "$DIREXION TESLA 2X: possibly delisted; no price data found "
                ' (period=1y) (Yahoo error = "No data found, symbol may be '
                'delisted")'
            ),
            "attributes": {"logger.name": "yfinance"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is None

    def test_before_send_log_keeps_real_yfinance_errors(self):
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": "Failed to decode JSON response from Yahoo Finance",
            "attributes": {"logger.name": "yfinance"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is not None

    def test_before_send_log_keeps_non_yfinance_logs(self):
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": "Trade executed successfully",
            "attributes": {"logger.name": "app.services.trading"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is not None

    def test_before_send_log_drops_yfinance_crumb_error(self):
        """yfinance crumb/auth errors are dropped from structured logs too."""
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": (
                'HTTP Error 401: {"finance":{"result":null,"error":'
                '{"code":"Unauthorized","description":"Invalid Crumb"}}}'
            ),
            "attributes": {"logger.name": "yfinance"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is None


@pytest.mark.unit
class TestFastmcpToolValidationFilter:
    """FastMCP tool validation errors are expected LLM client noise."""

    def test_fastmcp_validation_error_event_dropped(self):
        """cancel_order with wrong param name → dropped."""
        event: Event = {
            "logger": "fastmcp.server.server",
            "logentry": {
                "message": "Error validating tool 'cancel_order'",
                "formatted": "Error validating tool 'cancel_order'",
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_validation_error_from_log_record_dropped(self):
        """Validation error detected via hint log_record (mechanism=logging)."""
        import logging

        record = logging.LogRecord(
            name="fastmcp.server.server",
            level=logging.ERROR,
            pathname="fastmcp/server/server.py",
            lineno=987,
            msg="Error validating tool '%s'",
            args=("place_order",),
            exc_info=None,
        )
        event: Event = {}
        hint = {"log_record": record}
        assert sentry_module._before_send(event, hint) is None

    def test_fastmcp_non_validation_error_kept(self):
        """FastMCP errors that are NOT validation → kept."""
        event: Event = {
            "logger": "fastmcp.server.server",
            "logentry": {
                "message": "Unexpected error in tool execution",
                "formatted": "Unexpected error in tool execution",
            },
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_non_fastmcp_logger_kept(self):
        """Validation-like message from a different logger → kept."""
        event: Event = {
            "logger": "app.services",
            "logentry": {
                "message": "Error validating tool 'cancel_order'",
                "formatted": "Error validating tool 'cancel_order'",
            },
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_before_send_log_drops_fastmcp_validation(self):
        """Sentry structured log path also filters validation noise."""
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": "Error validating tool 'get_holdings'",
            "attributes": {"logger.name": "fastmcp.server.server"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is None

    def test_before_send_log_keeps_fastmcp_non_validation(self):
        """Non-validation FastMCP log → kept."""
        sentry_log: Log = {
            "severity_text": "error",
            "severity_number": 17,
            "body": "Tool execution failed with timeout",
            "attributes": {"logger.name": "fastmcp.server.server"},
            "time_unix_nano": 1,
            "trace_id": None,
            "span_id": None,
        }
        assert sentry_module._before_send_log(sentry_log, {}) is not None


@pytest.mark.unit
class TestGetOrderHistorySentryNoiseFilter:
    """get_order_history symbol requirement noise should be dropped in Sentry."""

    def test_fastmcp_get_order_history_symbol_requirement_log_dropped(self):
        """AUTO_TRADER-41: log path noise dropped."""
        event: Event = {
            "logger": "fastmcp.server.server",
            "logentry": {
                "message": (
                    "Error calling tool 'get_order_history': symbol is required when "
                    "status='filled'. Use status='pending' for symbol-free queries, "
                    "or provide a symbol (e.g. symbol='KRW-BTC')."
                ),
                "formatted": (
                    "Error calling tool 'get_order_history': symbol is required when "
                    "status='filled'. Use status='pending' for symbol-free queries, "
                    "or provide a symbol (e.g. symbol='KRW-BTC')."
                ),
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_get_order_history_symbol_requirement_toolerror_dropped(self):
        """AUTO_TRADER-40: ToolError exception path noise dropped."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": (
                            "Error calling tool 'get_order_history': symbol is required "
                            "when status='filled'. Use status='pending' for symbol-free "
                            "queries, or provide a symbol (e.g. symbol='KRW-BTC')."
                        ),
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_get_order_history_symbol_requirement_valueerror_dropped(self):
        """ValueError path (direct implementation throw) also dropped."""
        event: Event = {
            "contexts": {
                "mcp_tool_call": {
                    "tool_name": "get_order_history",
                    "arguments": {"status": "filled", "market": "crypto", "limit": 3},
                }
            },
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": (
                            "symbol is required when status='filled'. "
                            "Use status='pending' for symbol-free queries, "
                            "or provide a symbol (e.g. symbol='KRW-BTC')."
                        ),
                    }
                ]
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_real_runtime_error_kept(self):
        """Real runtime errors in get_order_history are still kept."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": "Error calling tool 'get_order_history': upstream timeout",
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_non_mcp_symbol_requirement_valueerror_kept(self):
        """Non-MCP exceptions with the same message fragment must not be dropped."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": (
                            "symbol is required when status='filled'. "
                            "Use status='pending' for symbol-free queries, "
                            "or provide a symbol (e.g. symbol='KRW-BTC')."
                        ),
                    }
                ]
            }
        }

        assert sentry_module._before_send(event, {}) is not None


@pytest.mark.unit
class TestGetShortInterestSentryNoiseFilter:
    """get_short_interest KR-only validation noise should be dropped in Sentry."""

    def test_fastmcp_get_short_interest_kr_only_log_dropped(self):
        """AUTO_TRADER-42: log path noise dropped."""
        event: Event = {
            "logger": "fastmcp.server.server",
            "logentry": {
                "message": (
                    "Error calling tool 'get_short_interest': "
                    "Short selling data is only available for Korean stocks "
                    "(6-digit codes like '005930')"
                ),
                "formatted": (
                    "Error calling tool 'get_short_interest': "
                    "Short selling data is only available for Korean stocks "
                    "(6-digit codes like '005930')"
                ),
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_get_short_interest_kr_only_toolerror_dropped(self):
        """AUTO_TRADER-43: ToolError exception path noise dropped."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": (
                            "Error calling tool 'get_short_interest': "
                            "Short selling data is only available for Korean stocks "
                            "(6-digit codes like '005930')"
                        ),
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_get_short_interest_kr_only_valueerror_dropped(self):
        """ValueError path (direct implementation throw) also dropped."""
        event: Event = {
            "contexts": {
                "mcp_tool_call": {
                    "tool_name": "get_short_interest",
                    "arguments": {"symbol": "SMCI", "days": 10},
                }
            },
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": (
                            "Short selling data is only available for Korean stocks "
                            "(6-digit codes like '005930')"
                        ),
                    }
                ]
            },
        }
        assert sentry_module._before_send(event, {}) is None

    def test_fastmcp_get_short_interest_runtime_error_kept(self):
        """Real runtime errors in get_short_interest are still kept."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": "Error calling tool 'get_short_interest': upstream timeout",
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is not None


@pytest.mark.unit
class TestStockOnlyFundamentalsSentryNoiseFilter:
    """Stock-only fundamentals tools should drop crypto validation noise in Sentry."""

    @pytest.mark.parametrize(
        ("tool_name", "message"),
        [
            (
                "get_company_profile",
                "Company profile is not available for cryptocurrencies",
            ),
            (
                "get_valuation",
                "Valuation metrics are not available for cryptocurrencies",
            ),
        ],
    )
    def test_fastmcp_stock_only_crypto_validation_log_dropped(self, tool_name, message):
        """AUTO_TRADER-45/47: log path noise dropped."""
        event: Event = {
            "logger": "fastmcp.server.server",
            "logentry": {
                "message": f"Error calling tool '{tool_name}': {message}",
                "formatted": f"Error calling tool '{tool_name}': {message}",
            },
        }
        assert sentry_module._before_send(event, {}) is None

    @pytest.mark.parametrize(
        ("tool_name", "message"),
        [
            (
                "get_company_profile",
                "Company profile is not available for cryptocurrencies",
            ),
            (
                "get_valuation",
                "Valuation metrics are not available for cryptocurrencies",
            ),
        ],
    )
    def test_fastmcp_stock_only_crypto_validation_toolerror_dropped(
        self, tool_name, message
    ):
        """AUTO_TRADER-46/48: ToolError exception path noise dropped."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": f"Error calling tool '{tool_name}': {message}",
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is None

    @pytest.mark.parametrize(
        ("tool_name", "message"),
        [
            (
                "get_company_profile",
                "Company profile is not available for cryptocurrencies",
            ),
            (
                "get_valuation",
                "Valuation metrics are not available for cryptocurrencies",
            ),
        ],
    )
    def test_stock_only_crypto_validation_valueerror_dropped(self, tool_name, message):
        """Direct ValueError path (implementation throw) also dropped."""
        event: Event = {
            "contexts": {
                "mcp_tool_call": {
                    "tool_name": tool_name,
                    "arguments": {"symbol": "KRW-BTC"},
                }
            },
            "exception": {"values": [{"type": "ValueError", "value": message}]},
        }
        assert sentry_module._before_send(event, {}) is None

    @pytest.mark.parametrize("tool_name", ["get_company_profile", "get_valuation"])
    def test_stock_only_runtime_errors_kept(self, tool_name):
        """Runtime errors (upstream timeout) should NOT be dropped."""
        event: Event = {
            "exception": {
                "values": [
                    {
                        "type": "ToolError",
                        "value": f"Error calling tool '{tool_name}': upstream timeout",
                    }
                ]
            }
        }
        assert sentry_module._before_send(event, {}) is not None
