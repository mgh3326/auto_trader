import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.middleware.monitoring import MonitoringMiddleware
from app.monitoring.error_reporter import ErrorReporter


@pytest.fixture
def error_reporter():
    """Provide singleton error reporter."""
    return ErrorReporter()


def test_generate_rate_limit_key_uses_first_frame_for_signature(error_reporter):
    stack_trace = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/app/main.py", line 10, in <module>',
            "    raise ValueError('Boom')",
            "ValueError: Boom",
        ]
    )

    result = error_reporter._generate_rate_limit_key(
        error_type="ValueError",
        error_message="Boom",
        stack_trace=stack_trace,
    )

    expected_signature = (
        'ValueError:Boom:File "/app/main.py", line 10, in <module>'
    )
    expected_hash = hashlib.sha256(expected_signature.encode()).hexdigest()

    assert result == f"error_rate_limit:{expected_hash}"


def test_format_error_message_truncates_long_payload(error_reporter):
    long_stack_trace = "\n".join(["frame"] * 5000)

    message = error_reporter._format_error_message(
        error_type="RuntimeError",
        error_message="A very long error message",
        stack_trace=long_stack_trace,
    )

    assert len(message) <= 4000
    assert "... (truncated)" in message
    assert "*Stack Trace:*" in message


def test_record_metrics_updates_histogram_and_counter():
    middleware = MonitoringMiddleware(MagicMock())
    middleware._meter = object()
    histogram = MagicMock()
    counter = MagicMock()
    middleware._request_duration_histogram = histogram
    middleware._request_counter = counter

    request = MagicMock()
    request.method = "POST"
    request.url = SimpleNamespace(path="/api/test")

    middleware._record_metrics(request, status_code=201, duration_ms=123.45)

    expected_attributes = {
        "http.method": "POST",
        "http.route": "/api/test",
        "http.status_code": "201",
    }

    histogram.record.assert_called_once_with(123.45, expected_attributes)
    counter.add.assert_called_once_with(1, expected_attributes)


@pytest.mark.asyncio
async def test_handle_http_exception_reports_and_returns_json(monkeypatch):
    middleware = MonitoringMiddleware(MagicMock())

    class DummyReporter:
        def __init__(self):
            self.calls = 0
            self.last_context = None

        async def send_error_to_telegram(self, *args, **kwargs):
            self.calls += 1
            self.last_context = kwargs.get("additional_context")
            return True

    reporter = DummyReporter()
    monkeypatch.setattr(
        "app.middleware.monitoring.get_error_reporter",
        lambda: reporter,
    )

    class DummyURL:
        def __init__(self, path: str):
            self.path = path

        def __str__(self):
            return f"http://testserver{self.path}"

    request = MagicMock()
    request.method = "GET"
    request.url = DummyURL("/api/test")
    request.client = SimpleNamespace(host="127.0.0.1", port=8000)
    request.headers = {"user-agent": "pytest"}

    exc = HTTPException(status_code=500, detail="boom", headers={"X-Test": "1"})
    start_time = time.time() - 0.01

    response = await middleware._handle_http_exception(
        request, exc, start_time, request_id="req-1"
    )

    body = json.loads(response.body)

    assert response.status_code == 500
    assert body["detail"] == "boom"
    assert body["request_id"] == "req-1"
    assert response.headers["X-Request-ID"] == "req-1"
    assert response.headers["X-Test"] == "1"
    assert "X-Process-Time" in response.headers

    assert reporter.calls == 1
    assert reporter.last_context["status_code"] == 500
    assert reporter.last_context["request_id"] == "req-1"
    assert isinstance(reporter.last_context["duration_ms"], float)
