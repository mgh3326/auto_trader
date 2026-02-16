from __future__ import annotations

from typing import Any

import pytest
import sentry_sdk
from curl_cffi.requests import Session

from app.monitoring.yfinance_sentry import SentryTracingCurlSession


class _DummyResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _DummySpan:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value


class _DummySpanContext:
    def __init__(self, span: _DummySpan) -> None:
        self._span = span

    def __enter__(self) -> _DummySpan:
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_tracing_session_uses_method_and_path(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SentryTracingCurlSession()
    started: list[tuple[str, str, _DummySpan]] = []

    def fake_start_span(op: str, name: str) -> _DummySpanContext:
        span = _DummySpan()
        started.append((op, name, span))
        return _DummySpanContext(span)

    def fake_parent_request(self, method, url, **kwargs):
        del self, method, url, kwargs
        return _DummyResponse(status_code=200)

    monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
    monkeypatch.setattr(Session, "request", fake_parent_request)

    response = session.request(
        "GET",
        "https://query1.finance.yahoo.com/v1/finance/screener?x=1",
    )

    assert response.status_code == 200
    assert len(started) == 1
    op, name, span = started[0]
    assert op == "http.client"
    assert name == "GET /v1/finance/screener"
    assert (
        span.data["url"] == "https://query1.finance.yahoo.com/v1/finance/screener?x=1"
    )
    assert span.data["http.request.method"] == "GET"
    assert span.data["http.response.status_code"] == 200


def test_tracing_session_records_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SentryTracingCurlSession()
    started: list[tuple[str, str, _DummySpan]] = []

    def fake_start_span(op: str, name: str) -> _DummySpanContext:
        span = _DummySpan()
        started.append((op, name, span))
        return _DummySpanContext(span)

    def fake_parent_request(self, method, url, **kwargs):
        del self, method, url, kwargs
        return _DummyResponse(status_code=204)

    monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
    monkeypatch.setattr(Session, "request", fake_parent_request)

    session.request(
        "post", "https://query2.finance.yahoo.com/v10/finance/quoteSummary/AAPL"
    )

    assert started[0][2].data["http.response.status_code"] == 204


def test_tracing_session_invalid_url_uses_unknown_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SentryTracingCurlSession()
    started: list[tuple[str, str, _DummySpan]] = []

    def fake_start_span(op: str, name: str) -> _DummySpanContext:
        span = _DummySpan()
        started.append((op, name, span))
        return _DummySpanContext(span)

    def fake_parent_request(self, method, url, **kwargs):
        del self, method, url, kwargs
        return _DummyResponse(status_code=200)

    monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
    monkeypatch.setattr(Session, "request", fake_parent_request)

    session.request("GET", None)

    assert started[0][1] == "GET /unknown"


def test_tracing_session_reraises_request_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SentryTracingCurlSession()

    def fake_start_span(op: str, name: str) -> _DummySpanContext:
        del op, name
        return _DummySpanContext(_DummySpan())

    def fake_parent_request(self, method, url, **kwargs):
        del self, method, url, kwargs
        raise RuntimeError("request boom")

    monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
    monkeypatch.setattr(Session, "request", fake_parent_request)

    with pytest.raises(RuntimeError, match="request boom"):
        session.request("GET", "https://query1.finance.yahoo.com/v1/test")


def test_tracing_session_forwards_kwargs_to_parent_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SentryTracingCurlSession()
    captured: dict[str, Any] = {}

    def fake_start_span(op: str, name: str) -> _DummySpanContext:
        del op, name
        return _DummySpanContext(_DummySpan())

    def fake_parent_request(self, method, url, **kwargs):
        del self
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _DummyResponse(status_code=200)

    monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
    monkeypatch.setattr(Session, "request", fake_parent_request)

    headers = {"x-test": "1"}
    params = {"a": 1}
    timeout = 4.2

    session.request(
        "GET",
        "https://query1.finance.yahoo.com/v1/finance/screener",
        headers=headers,
        params=params,
        timeout=timeout,
    )

    assert captured["method"] == "GET"
    assert captured["url"] == "https://query1.finance.yahoo.com/v1/finance/screener"
    assert captured["kwargs"] == {
        "headers": headers,
        "params": params,
        "timeout": timeout,
    }
