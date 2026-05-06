from __future__ import annotations

import urllib.error
from typing import Any

import pytest
import sentry_sdk
from curl_cffi.requests import Session

import app.monitoring.yfinance_sentry as yfinance_sentry_module
from app.monitoring.yfinance_sentry import SentryTracingCurlSession
from app.services.brokers.yahoo.client import (
    _fetch_fast_info_sync,
    fetch_fundamental_info,
)


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


def test_build_tracing_session_uses_chrome_impersonation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs

    monkeypatch.setattr(
        yfinance_sentry_module, "SentryTracingCurlSession", _FakeSession
    )

    session = yfinance_sentry_module.build_yfinance_tracing_session()

    assert isinstance(session, _FakeSession)
    assert captured["kwargs"] == {"impersonate": "chrome"}


class TestYahooRetryOnCrumbError:
    """Yahoo client retries on 401 Invalid Crumb with fresh session."""

    def test_retries_on_http_401_and_succeeds(self, monkeypatch):
        """First call raises 401, second call succeeds with new session."""
        call_count = 0
        sessions_created = []

        def fake_build_session():
            session = object()
            sessions_created.append(session)
            return session

        def fake_ticker(symbol, session=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(
                    url="https://query2.finance.yahoo.com/v8/finance/chart/BRK-B",
                    code=401,
                    msg='{"finance":{"error":{"description":"Invalid Crumb"}}}',
                    hdrs=None,
                    fp=None,
                )

            # Second call succeeds
            class FakeInfo:
                regular_market_previous_close = 100.0
                open = 101.0
                day_high = 102.0
                day_low = 99.0
                last_price = 101.5
                last_volume = 1000

                def get(self, key):
                    return None

            class FakeTicker:
                fast_info = FakeInfo()

            return FakeTicker()

        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            fake_build_session,
        )
        monkeypatch.setattr("app.services.brokers.yahoo.client.yf.Ticker", fake_ticker)

        result = _fetch_fast_info_sync("BRK.B")
        assert result["close"] == pytest.approx(101.5)
        assert call_count == 2
        assert len(sessions_created) == 2  # fresh session for retry

    def test_does_not_retry_on_non_401_error(self, monkeypatch):
        """Non-401 errors are raised immediately without retry."""
        call_count = 0

        def fake_build_session():
            return object()

        def fake_ticker(symbol, session=None):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Network unreachable")

        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            fake_build_session,
        )
        monkeypatch.setattr("app.services.brokers.yahoo.client.yf.Ticker", fake_ticker)

        with pytest.raises(ConnectionError, match="Network unreachable"):
            _fetch_fast_info_sync("AAPL")
        assert call_count == 1  # no retry

    def test_raises_after_max_retries_exhausted(self, monkeypatch):
        """After max retries, the original error is raised."""
        call_count = 0

        def fake_build_session():
            return object()

        def fake_ticker(symbol, session=None):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                url="https://query2.finance.yahoo.com/v8/finance/chart/BRK-B",
                code=401,
                msg="Invalid Crumb",
                hdrs=None,
                fp=None,
            )

        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            fake_build_session,
        )
        monkeypatch.setattr("app.services.brokers.yahoo.client.yf.Ticker", fake_ticker)

        with pytest.raises(urllib.error.HTTPError):
            _fetch_fast_info_sync("BRK.B")
        assert call_count == 2  # initial + 1 retry


class TestFetchFundamentalInfoRetry:
    @pytest.mark.asyncio
    async def test_retries_on_crumb_error(self, monkeypatch):
        call_count = 0

        def fake_build_session():
            return object()

        def fake_ticker(symbol, session=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(
                    url="https://query2.finance.yahoo.com/v10/finance/quoteSummary/AAPL",
                    code=401,
                    msg="Invalid Crumb",
                    hdrs=None,
                    fp=None,
                )

            class FakeTicker:
                info = {
                    "trailingPE": 25.0,
                    "priceToBook": 8.5,
                    "trailingEps": 6.0,
                    "bookValue": 4.0,
                    "trailingAnnualDividendYield": 0.005,
                }

            return FakeTicker()

        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            fake_build_session,
        )
        monkeypatch.setattr("app.services.brokers.yahoo.client.yf.Ticker", fake_ticker)

        result = await fetch_fundamental_info("AAPL")
        assert result["PER"] == pytest.approx(25.0)
        assert call_count == 2
