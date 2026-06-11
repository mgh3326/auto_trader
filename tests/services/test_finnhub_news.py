"""Finnhub news fetch retry/backoff/timeout (ROB-510)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from app.services import finnhub_news


def _client_with(side_effects):
    """company_news가 side_effects를 순서대로 내는 가짜 SDK 클라이언트."""
    client = MagicMock()
    client.company_news = MagicMock(side_effect=side_effects)
    client.general_news = MagicMock(side_effect=side_effects)
    return client


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """테스트에서 백오프 대기 제거."""
    from tenacity import wait_none

    monkeypatch.setattr(finnhub_news, "FINNHUB_NEWS_RETRY_WAIT", wait_none())


_OK_ITEM = [
    {
        "headline": "t",
        "source": "s",
        "datetime": 1765400000,
        "url": "https://u",
        "summary": "",
        "related": "AAPL",
    }
]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_network_error_is_retried_then_succeeds(monkeypatch):
    client = _client_with([requests.ConnectionError("boom"), _OK_ITEM])
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 5)

    assert result["count"] == 1
    assert client.company_news.call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exhausted_retries_reraise_original_error(monkeypatch):
    client = _client_with([requests.ConnectionError("boom")] * 5)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    with pytest.raises(requests.ConnectionError):
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 5, max_attempts=3)
    assert client.company_news.call_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_attempt_timeout_is_retried(monkeypatch):
    calls = {"n": 0}

    def slow_then_ok(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            import time

            time.sleep(0.5)  # timeout_s=0.05보다 길게 — 첫 시도 TimeoutError
        return _OK_ITEM

    client = MagicMock()
    client.company_news = MagicMock(side_effect=slow_then_ok)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    result = await finnhub_news.fetch_news_finnhub(
        "AAPL", "us", 5, timeout_s=0.05, max_attempts=2
    )

    assert result["count"] == 1
    assert calls["n"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_retryable_4xx_fails_immediately(monkeypatch):
    class FakeAPIError(Exception):
        status_code = 401

    monkeypatch.setattr(
        finnhub_news, "_is_retryable_news_error", finnhub_news._is_retryable_news_error
    )
    client = _client_with([FakeAPIError("unauthorized")] * 3)
    monkeypatch.setattr(finnhub_news, "_get_finnhub_client", lambda: client)

    with pytest.raises(FakeAPIError):
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 5, max_attempts=3)
    assert client.company_news.call_count == 1  # 4xx는 비재시도


@pytest.mark.unit
def test_retryable_classifier():
    assert finnhub_news._is_retryable_news_error(TimeoutError()) is True
    assert finnhub_news._is_retryable_news_error(requests.ReadTimeout()) is True
    assert finnhub_news._is_retryable_news_error(ValueError("no key")) is False
    assert finnhub_news._is_retryable_news_error(ImportError("no sdk")) is False

    if finnhub_news.finnhub is not None:
        exc5xx = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc5xx.status_code = 503
        assert finnhub_news._is_retryable_news_error(exc5xx) is True
        exc429 = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc429.status_code = 429
        assert finnhub_news._is_retryable_news_error(exc429) is True
        exc4xx = finnhub_news.finnhub.FinnhubAPIException.__new__(
            finnhub_news.finnhub.FinnhubAPIException
        )
        exc4xx.status_code = 403
        assert finnhub_news._is_retryable_news_error(exc4xx) is False
