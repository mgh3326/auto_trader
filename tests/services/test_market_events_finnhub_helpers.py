"""Tests for finnhub_helpers 429 mapping (ROB-264) and ROB-1384 retry/timeout."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from tenacity import wait_none

from app.services.market_events import finnhub_helpers


class _FakeFinnhubAPIException(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch):
    """테스트에서 백오프 대기 제거 (ROB-510 패턴과 동일)."""
    monkeypatch.setattr(finnhub_helpers, "FINNHUB_EARNINGS_RETRY_WAIT", wait_none())


def _client_with(side_effects) -> MagicMock:
    client = MagicMock()
    client.earnings_calendar = MagicMock(side_effect=side_effects)
    return client


_EARNINGS_ROW = {
    "earningsCalendar": [
        {
            "symbol": "AAPL",
            "date": "2026-05-14",
            "hour": "amc",
            "epsEstimate": 1.2,
            "epsActual": None,
            "revenueEstimate": None,
            "revenueActual": None,
            "quarter": 2,
            "year": 2026,
        }
    ]
}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_maps_429_to_quota_error(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=429,
        message="API limit reached. Please try again later. Remaining Limit: 0",
    )
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: fake_client)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-07-17"
        )

    assert exc_info.value.status_code == 429
    assert "API limit" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_passes_through_non_429(monkeypatch):
    from app.services.market_events import finnhub_helpers

    fake_client = MagicMock()
    fake_client.earnings_calendar.side_effect = _FakeFinnhubAPIException(
        status_code=500, message="upstream boom"
    )
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: fake_client)

    with pytest.raises(_FakeFinnhubAPIException):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-05-11"
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_retries_transient_then_succeeds(monkeypatch):
    client = _client_with([requests.ConnectionError("boom"), _EARNINGS_ROW])
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-07-17"
    )

    assert client.earnings_calendar.call_count == 2
    assert result["count"] == 1
    assert result["earnings"][0]["eps_estimate"] == 1.2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_exhausted_retries_reraise(monkeypatch):
    client = _client_with([requests.ConnectionError("boom")] * 5)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(requests.ConnectionError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert client.earnings_calendar.call_count == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_per_attempt_timeout_is_retried(monkeypatch):
    """ROB-1384 r4 (a)+(b) — a timed-out attempt is abandoned on its own
    client; the retry runs on a DIFFERENT client instance whose
    DEFAULT_TIMEOUT is the per-attempt bound, and its result is used."""
    calls = {"n": 0}
    clients: list[MagicMock] = []

    def slow_then_ok(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.08)  # outlives the 0.05s attempt timeout — abandoned
        return _EARNINGS_ROW

    def _client_factory() -> MagicMock:
        client = MagicMock()
        client.earnings_calendar = MagicMock(side_effect=slow_then_ok)
        clients.append(client)
        return client

    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", _client_factory)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.05, max_attempts=2
    )

    assert result["count"] == 1
    assert calls["n"] == 2
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert all(client.DEFAULT_TIMEOUT == 0.05 for client in clients)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_quota_error_is_not_retried(monkeypatch):
    client = _client_with(
        [_FakeFinnhubAPIException(status_code=429, message="limit")] * 3
    )
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert client.earnings_calendar.call_count == 1  # 쿼터는 절대 재시도 안 함


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_non_retryable_error_fails_once(monkeypatch):
    client = _client_with([ValueError("no api key")] * 3)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(ValueError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert client.earnings_calendar.call_count == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_aligns_sdk_timeout_with_attempt_budget(monkeypatch):
    """ROB-1384 — the SDK's own request timeout is bound to the per-attempt
    budget so an abandoned worker thread's socket ops stay capped."""
    seen: dict[str, Any] = {}

    class FakeClient:
        def earnings_calendar(self, **_kwargs):
            seen["timeout"] = getattr(self, "DEFAULT_TIMEOUT", None)
            return _EARNINGS_ROW

    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", FakeClient)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.5
    )

    assert result["count"] == 1
    assert seen["timeout"] == 0.5


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("status,expected_calls", [(403, 1), (503, 3)])
async def test_earnings_4xx_fails_once_5xx_uses_all_attempts(
    monkeypatch, status, expected_calls
):
    """ROB-1384 r4 (d) — real SDK 4xx errors are not retried; 5xx errors
    consume every attempt before reraising."""
    finnhub = pytest.importorskip("finnhub")

    def boom(**_kwargs):
        exc = finnhub.FinnhubAPIException.__new__(finnhub.FinnhubAPIException)
        exc.status_code = status
        raise exc

    client = _client_with(boom)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(finnhub.FinnhubAPIException):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert client.earnings_calendar.call_count == expected_calls


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_caller_cancellation_returns_without_waiting_for_thread(
    monkeypatch,
):
    """ROB-1384 r4 (e) — caller cancellation propagates immediately: the
    call returns well before the abandoned provider thread finishes."""
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking(**_kwargs):
        started.set()
        release.wait(10)
        finished.set()
        return _EARNINGS_ROW

    from types import SimpleNamespace

    client = SimpleNamespace(earnings_calendar=blocking)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    task = asyncio.ensure_future(
        finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=5.0, max_attempts=1
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        begin = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = asyncio.get_running_loop().time() - begin
        # Cancellation returned immediately — the provider thread is
        # still blocked on `release` (abandoned, not drained).
        assert elapsed < 1.0
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_all_timeouts_call_exactly_max_attempts_with_message(
    monkeypatch,
):
    """ROB-1384 r4 (f) — every attempt timing out calls the provider
    exactly max_attempts times, then reraises a message-bearing
    TimeoutError."""
    calls = {"n": 0}

    def always_slow(**_kwargs):
        calls["n"] += 1
        time.sleep(0.06)  # outlives the 0.02s attempt timeout — abandoned
        return _EARNINGS_ROW

    client = _client_with(always_slow)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(TimeoutError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.02, max_attempts=3
        )

    assert calls["n"] == 3
    assert str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("bad_timeout", [0.0, -1.0])
async def test_earnings_non_positive_timeout_never_calls_provider(
    monkeypatch, bad_timeout
):
    """ROB-1384 r4 (g) — a degenerate timeout fails before any provider
    call."""
    client = _client_with([_EARNINGS_ROW])
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

    with pytest.raises(TimeoutError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL",
            "2026-05-11",
            "2026-05-11",
            timeout_s=bad_timeout,
            max_attempts=1,
        )

    assert client.earnings_calendar.call_count == 0


@pytest.mark.unit
def test_earnings_retryable_classifier():
    h = finnhub_helpers
    assert h._is_retryable_earnings_error(TimeoutError()) is True
    assert h._is_retryable_earnings_error(requests.ConnectionError()) is True
    assert h._is_retryable_earnings_error(ValueError("x")) is False
    assert h._is_retryable_earnings_error(ImportError("x")) is False
    assert h._is_retryable_earnings_error(h.FinnhubQuotaExceededError("q")) is False
    # 비공식 fake는 finnhub.FinnhubAPIException이 아니므로 비재시도
    assert h._is_retryable_earnings_error(_FakeFinnhubAPIException(503)) is False
    try:
        import finnhub
    except ImportError:
        return
    exc5xx = finnhub.FinnhubAPIException.__new__(finnhub.FinnhubAPIException)
    exc5xx.status_code = 503
    assert h._is_retryable_earnings_error(exc5xx) is True
    exc4xx = finnhub.FinnhubAPIException.__new__(finnhub.FinnhubAPIException)
    exc4xx.status_code = 403
    assert h._is_retryable_earnings_error(exc4xx) is False
