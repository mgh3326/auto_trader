"""Unit tests for the shared Upbit retry-with-backoff logic."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.async_rate_limiter import RateLimitExceededError


def _make_response(status_code: int, *, json_data=None, retry_after=None):
    """Helper: build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    if retry_after is not None:
        resp.headers["Retry-After"] = str(retry_after)
    if json_data is not None:
        resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code}",
            request=MagicMock(),
            response=MagicMock(status_code=status_code),
        )
    return resp


def _make_limiter():
    limiter = AsyncMock()
    limiter.acquire = AsyncMock()
    return limiter


@pytest.mark.asyncio
async def test_success_on_first_attempt():
    from app.services.brokers.upbit.client import _retry_with_backoff

    resp = _make_response(200, json_data=[{"market": "KRW-BTC"}])
    send_fn = AsyncMock(return_value=resp)

    result = await _retry_with_backoff(
        _make_limiter(), send_fn, url="https://test", max_retries=3, base_delay=0.01
    )

    assert result == [{"market": "KRW-BTC"}]
    assert send_fn.await_count == 1


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    from app.services.brokers.upbit.client import _retry_with_backoff

    resp_429 = _make_response(429, retry_after=0.01)
    resp_200 = _make_response(200, json_data={"ok": True})
    send_fn = AsyncMock(side_effect=[resp_429, resp_200])

    result = await _retry_with_backoff(
        _make_limiter(), send_fn, url="https://test", max_retries=3, base_delay=0.01
    )

    assert result == {"ok": True}
    assert send_fn.await_count == 2


@pytest.mark.asyncio
async def test_exhausts_retries_raises_rate_limit_error():
    from app.services.brokers.upbit.client import _retry_with_backoff

    resp_429 = _make_response(429)
    send_fn = AsyncMock(return_value=resp_429)

    with pytest.raises(RateLimitExceededError):
        await _retry_with_backoff(
            _make_limiter(), send_fn, url="https://test", max_retries=2, base_delay=0.01
        )

    assert send_fn.await_count == 3  # attempts 0, 1, 2


@pytest.mark.asyncio
async def test_retries_on_request_error_then_succeeds():
    from app.services.brokers.upbit.client import _retry_with_backoff

    resp_200 = _make_response(200, json_data={"ok": True})
    send_fn = AsyncMock(side_effect=[httpx.RequestError("connection reset"), resp_200])

    result = await _retry_with_backoff(
        _make_limiter(), send_fn, url="https://test", max_retries=3, base_delay=0.01
    )

    assert result == {"ok": True}
    assert send_fn.await_count == 2


@pytest.mark.asyncio
async def test_non_429_http_error_raises_immediately():
    from app.services.brokers.upbit.client import _retry_with_backoff

    resp_500 = _make_response(500)
    send_fn = AsyncMock(return_value=resp_500)

    with pytest.raises(httpx.HTTPStatusError):
        await _retry_with_backoff(
            _make_limiter(), send_fn, url="https://test", max_retries=3, base_delay=0.01
        )

    assert send_fn.await_count == 1  # no retry on 500
