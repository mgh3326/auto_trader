"""Tests for Sentry integration in FastAPI app creation and exception handling."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from starlette.requests import Request

import app.main as main_module
from app.monitoring.sentry import _before_send


def _build_request(path: str = "/boom") -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


@pytest.mark.unit
def test_create_app_initializes_sentry(monkeypatch):
    init_mock = Mock(return_value=True)
    monkeypatch.setattr(main_module, "init_sentry", init_mock)

    main_module.create_app()

    init_mock.assert_called_once_with(
        service_name="auto-trader-api",
        enable_fastapi=True,
        enable_sqlalchemy=True,
        enable_httpx=True,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_exception_handler_captures_to_sentry(monkeypatch):
    monkeypatch.setattr(main_module, "init_sentry", Mock(return_value=True))
    capture_mock = Mock()
    monkeypatch.setattr(main_module, "capture_exception", capture_mock)

    app = main_module.create_app()
    handler = app.exception_handlers[Exception]
    request = _build_request()
    error = RuntimeError("boom")

    response = await handler(request, error)

    assert response.status_code == 500
    capture_mock.assert_called_once()
    called_error = capture_mock.call_args.args[0]
    assert isinstance(called_error, RuntimeError)
    assert str(called_error) == "boom"
    assert capture_mock.call_args.kwargs["path"] == "/boom"
    assert capture_mock.call_args.kwargs["method"] == "GET"


class TestYfinanceLogEventFiltering:
    """yfinance internal logger ERROR events are filtered from Sentry."""

    def test_yfinance_invalid_crumb_event_is_dropped(self):
        """yfinance logger 'Invalid Crumb' error should not create Sentry event."""
        event = {
            "logger": "yfinance",
            "message": 'HTTP Error 401: {"finance":{"error":{"description":"Invalid Crumb"}}}',
            "level": "error",
        }
        hint: dict = {
            "log_record": type(
                "LogRecord",
                (),
                {
                    "name": "yfinance",
                    "getMessage": lambda self: event["message"],
                },
            )(),
        }
        result = _before_send(event, hint)
        assert result is None  # dropped

    def test_yfinance_invalid_cookie_event_is_dropped(self):
        """yfinance logger 'Invalid Cookie' error should not create Sentry event."""
        event = {
            "logger": "yfinance",
            "message": 'HTTP Error 401: {"finance":{"error":{"description":"Invalid Cookie"}}}',
            "level": "error",
        }
        hint: dict = {
            "log_record": type(
                "LogRecord",
                (),
                {
                    "name": "yfinance",
                    "getMessage": lambda self: event["message"],
                },
            )(),
        }
        result = _before_send(event, hint)
        assert result is None

    def test_non_yfinance_401_event_is_kept(self):
        """Non-yfinance 401 errors should still be captured."""
        event = {
            "logger": "app.services.kis",
            "message": "HTTP 401 Unauthorized",
            "level": "error",
        }
        hint: dict = {
            "log_record": type(
                "LogRecord",
                (),
                {
                    "name": "app.services.kis",
                    "getMessage": lambda self: event["message"],
                },
            )(),
        }
        result = _before_send(event, hint)
        assert result is not None  # kept

    def test_yfinance_non_crumb_error_is_kept(self):
        """yfinance errors that are NOT crumb/cookie related should be kept."""
        event = {
            "logger": "yfinance",
            "message": "Connection timeout for AAPL",
            "level": "error",
        }
        hint: dict = {
            "log_record": type(
                "LogRecord",
                (),
                {
                    "name": "yfinance",
                    "getMessage": lambda self: event["message"],
                },
            )(),
        }
        result = _before_send(event, hint)
        assert result is not None  # kept
