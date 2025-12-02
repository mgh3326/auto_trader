import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.middleware.monitoring import MonitoringMiddleware
from app.monitoring.error_reporter import ErrorReporter, escape_markdown


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


class TestEscapeMarkdown:
    """escape_markdown 함수 테스트."""

    def test_escape_underscore(self):
        """언더스코어(_)가 이스케이프되어야 함."""
        text = "process_kis_domestic_sell_orders"
        result = escape_markdown(text)
        assert result == r"process\_kis\_domestic\_sell\_orders"

    def test_escape_asterisk(self):
        """별표(*)가 이스케이프되어야 함."""
        text = "**bold** and *italic*"
        result = escape_markdown(text)
        assert r"\*\*bold\*\*" in result
        assert r"\*italic\*" in result

    def test_escape_backtick(self):
        """백틱(`)이 이스케이프되어야 함."""
        text = "use `code` here"
        result = escape_markdown(text)
        assert r"\`code\`" in result

    def test_escape_square_bracket(self):
        """대괄호([)가 이스케이프되어야 함."""
        text = "see [link] here"
        result = escape_markdown(text)
        assert r"\[link]" in result

    def test_no_escape_for_normal_text(self):
        """특수문자가 없는 일반 텍스트는 변경되지 않아야 함."""
        text = "APBK0400 주문 가능한 수량을 초과했습니다."
        result = escape_markdown(text)
        assert result == text

    def test_complex_error_message(self):
        """실제 에러 메시지 시나리오."""
        text = "File '/app/services/kis_trading_service.py', line 343"
        result = escape_markdown(text)
        # 언더스코어만 이스케이프됨
        assert r"kis\_trading\_service" in result


class TestFormatErrorMessageWithEscape:
    """에러 메시지 포맷팅 시 이스케이프 테스트."""

    def test_format_message_escapes_error_message(self, error_reporter):
        """에러 메시지의 특수문자가 이스케이프되어야 함."""
        message = error_reporter._format_error_message(
            error_type="RuntimeError",
            error_message="Error in func_name with _underscore",
            stack_trace="simple trace",
        )
        # 에러 메시지 부분에서 언더스코어가 이스케이프되어야 함
        assert r"func\_name" in message
        assert r"\_underscore" in message

    def test_format_message_escapes_additional_context(self, error_reporter):
        """추가 컨텍스트의 특수문자가 이스케이프되어야 함."""
        message = error_reporter._format_error_message(
            error_type="RuntimeError",
            error_message="Error",
            stack_trace="trace",
            additional_context={
                "task_name": "kis.run_per_domestic_stock_automation",
                "stock": "삼성전자우 (005935)"
            }
        )
        # task_name의 언더스코어가 이스케이프되어야 함
        assert r"run\_per\_domestic\_stock\_automation" in message
