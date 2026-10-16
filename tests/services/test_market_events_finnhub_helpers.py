"""Tests for finnhub_helpers 429 mapping (ROB-264) and ROB-1384 retry/timeout."""

from __future__ import annotations

import time
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
            time.sleep(0.3)  # timeout_s=0.05보다 길게 — 첫 시도 TimeoutError
        return _EARNINGS_ROW

    client = MagicMock()
    client.earnings_calendar = MagicMock(side_effect=slow_then_ok)
    monkeypatch.setattr(finnhub_helpers, "_get_finnhub_client", lambda: client)

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
