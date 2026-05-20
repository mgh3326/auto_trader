"""ROB-270: ReadTimeout/RequestError retry vs 429 retry separation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis.base import BaseKISClient


class _FakeSettings:
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 2  # → max 3 attempts total
    api_rate_limit_retry_429_base_delay = 0.01


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FakeSettings()


def _make_client() -> _FakeClient:
    return _FakeClient()


def _patch_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_limiter(*args, **kwargs):
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        return limiter

    monkeypatch.setattr("app.services.brokers.kis.base.get_limiter", _fake_get_limiter)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_retries_read_timeout_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: Default behavior unchanged — ReadTimeout still retries."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    with pytest.raises(httpx.RequestError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
        )

    # api_rate_limit_retry_429_max = 2 → 3 attempts
    assert call_count["n"] == 3, (
        f"Default should retry ReadTimeout 3 times, got {call_count['n']}"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_does_not_retry_read_timeout_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: With retry_request_errors=False, ReadTimeout raises after 1 try."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    with pytest.raises(httpx.RequestError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
            retry_request_errors=False,
        )

    assert call_count["n"] == 1, (
        "retry_request_errors=False must short-circuit RequestError retries; "
        f"got {call_count['n']} attempts"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_request_helper_retries_429_even_when_request_errors_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: 429 retry path is independent of retry_request_errors."""
    _patch_limiter(monkeypatch)
    client = _make_client()

    call_count = {"n": 0}

    async def _fake_execute(*args, **kwargs):
        call_count["n"] += 1
        # Build a fake httpx.Response carrying 429 status
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "0"}
        # When code branches into _parse_kis_response, it won't run because
        # status_code == 429 is handled by the explicit branch first.
        return response

    monkeypatch.setattr(client, "_execute_http_request", _fake_execute)

    async def _fake_ensure_client(timeout=None):
        return MagicMock()

    monkeypatch.setattr(client, "_ensure_client", _fake_ensure_client)

    # 3 attempts then RateLimitExceededError
    from app.core.async_rate_limiter import RateLimitExceededError

    with pytest.raises(RateLimitExceededError):
        await client._request_with_rate_limit_with_headers(
            "GET",
            "https://example.com/x",
            headers={},
            api_name="t",
            retry_request_errors=False,
        )

    assert call_count["n"] == 3, (
        "429 must still retry 3 times regardless of retry_request_errors; "
        f"got {call_count['n']}"
    )
