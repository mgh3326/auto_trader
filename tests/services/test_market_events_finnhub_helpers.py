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
    calls = {"n": 0}

    def slow_then_ok(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Outlives the 0.05s attempt timeout but finishes inside the
            # drain grace (monkeypatched wide for load-insensitive timing) —
            # a live worker is never overlapped.
            time.sleep(0.08)
        return _EARNINGS_ROW

    client = MagicMock()
    client.earnings_calendar = MagicMock(side_effect=slow_then_ok)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)
    monkeypatch.setattr(finnhub_helpers, "EARNINGS_DRAIN_GRACE_S", 5.0)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.05, max_attempts=2
    )

    assert result["count"] == 1
    assert calls["n"] == 2


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
async def test_earnings_timeout_drains_worker_before_next_attempt(monkeypatch):
    """ROB-1384 B2 — wait_for cancels only the awaiter; the worker thread must
    be drained to completion so retries never overlap a live provider call."""
    import threading

    active: set[int] = set()
    lock = threading.Lock()
    calls = {"n": 0}

    def blocking(**_kwargs):
        tid = threading.get_ident()
        with lock:
            active.add(tid)
            calls["n"] += 1
        try:
            # Outlives the 0.05s per-attempt timeout but finishes inside
            # the drain grace (monkeypatched wide — loaded hosts overshoot
            # time.sleep heavily) so the drain always completes first.
            time.sleep(0.08)
            return _EARNINGS_ROW
        finally:
            with lock:
                active.discard(tid)

    client = MagicMock()
    client.earnings_calendar = MagicMock(side_effect=blocking)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)
    monkeypatch.setattr(finnhub_helpers, "EARNINGS_DRAIN_GRACE_S", 5.0)

    with pytest.raises(TimeoutError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.05, max_attempts=2
        )

    assert calls["n"] == 2
    with lock:
        assert not active, "provider workers must not outlive the call"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_timeout_drain_preserves_late_429_and_never_retries(
    monkeypatch,
):
    """ROB-1384 R2-F1 — a 429 arriving during the bounded drain keeps its own
    classification: it must surface as FinnhubQuotaExceededError and must
    never be retried, not be discarded and replayed as a bare TimeoutError."""
    import threading

    release = threading.Event()

    def late_quota(**_kwargs):
        # Outlives the 0.02s attempt timeout; the monkeypatched-wide drain
        # grace keeps this deterministic even when sleep overshoots.
        release.wait(0.05)
        raise _FakeFinnhubAPIException(status_code=429)

    client = _client_with(late_quota)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)
    monkeypatch.setattr(finnhub_helpers, "EARNINGS_DRAIN_GRACE_S", 5.0)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.02, max_attempts=3
        )

    assert client.earnings_calendar.call_count == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_timeout_bounded_drain_tracks_outliving_worker(
    monkeypatch,
):
    """ROB-1384 R2-F2 — the drain is bounded: a worker that outlives the
    grace ends the call promptly (non-retryably) and stays tracked until
    its thread finishes, instead of blocking the deadline or being orphaned."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_worker(**_kwargs):
        started.set()
        release.wait(5)  # far beyond attempt timeout + drain grace
        return _EARNINGS_ROW

    client = _client_with(slow_worker)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)
    monkeypatch.setattr(finnhub_helpers, "EARNINGS_DRAIN_GRACE_S", 0.02)

    with pytest.raises(finnhub_helpers.ProviderWorkerStillRunningError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.02, max_attempts=3
        )

    # Non-retryable so no second call can overlap the live worker.
    assert client.earnings_calendar.call_count == 1
    # Explicitly owned until its thread ends.
    assert finnhub_helpers._PENDING_PROVIDER_WORKERS
    try:
        for _worker in list(finnhub_helpers._PENDING_PROVIDER_WORKERS):
            assert started.is_set()
    finally:
        release.set()
        for worker in list(finnhub_helpers._PENDING_PROVIDER_WORKERS):
            await worker
        # done-callbacks run on the next loop tick
        await asyncio.sleep(0)
    assert not finnhub_helpers._PENDING_PROVIDER_WORKERS


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("during_drain", [False, True])
async def test_earnings_caller_cancellation_drains_worker_to_completion(
    monkeypatch,
    during_drain,
):
    """ROB-1384 R2-F2 — caller cancellation (before timeout or mid-drain)
    drains the provider thread to real completion before propagating;
    a live worker is never left behind."""
    import asyncio
    import threading

    started, release, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    active: set[int] = set()
    lock = threading.Lock()

    def blocking(**_kwargs):
        with lock:
            active.add(threading.get_ident())
        started.set()
        try:
            release.wait(2)
            return _EARNINGS_ROW
        finally:
            with lock:
                active.clear()
            finished.set()

    from types import SimpleNamespace

    client = SimpleNamespace(earnings_calendar=blocking)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)
    monkeypatch.setattr(finnhub_helpers, "EARNINGS_DRAIN_GRACE_S", 5.0)

    task = asyncio.ensure_future(
        finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL",
            "2026-05-11",
            "2026-05-11",
            timeout_s=0.02 if during_drain else 1.0,
            max_attempts=1,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if during_drain:
            await asyncio.sleep(0.05)  # land the cancel inside the drain
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with lock:
            assert not active, "cancellation must not leave a live worker"
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_aligns_sdk_timeout_with_attempt_budget(monkeypatch):
    """ROB-1384 B2 — the SDK's own request timeout is bound to the per-attempt
    budget so the worker thread cannot outlive it."""
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
