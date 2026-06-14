# ROB-530 Toss API Client Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only Toss Securities Open API foundation: fail-closed config, host-guarded transport, Redis single-flight OAuth, rate limiting, error parsing, typed read client, and disabled-by-default preflight smoke.

**Architecture:** Add a new `app/services/brokers/toss/` package that mirrors existing broker boundaries but keeps Toss isolated. The client is read-only: `/oauth2/token` is the only POST implemented in ROB-530, while account, asset, market data, market info, order history, and order-info reads use a shared transport/auth/rate-limit/error stack. Tokens are shared through Redis and issued under a single-flight lock because Toss invalidates the prior token when a client gets a new one.

**Tech Stack:** Python 3.13, httpx, redis.asyncio, Pydantic Settings v2, dataclasses, Decimal, pytest, pytest-asyncio, Ruff, ty.

---

## Starting State And Scope

ROB-530 is the first child of ROB-529 and blocks the Toss order, portfolio, exchange-rate, stock-master, warnings, calendar, and candle follow-ups. The source of truth is Toss OpenAPI v1.1.1 at `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`; the human docs route JavaScript clients to `/docs`, while `https://developers.tossinvest.com/llms.txt` points agents to the markdown overview and canonical OpenAPI JSON.

Hard scope:

- Create `app/services/brokers/toss/`.
- Add settings for `TOSS_API_ENABLED`, `TOSS_API_CLIENT_ID`, `TOSS_API_CLIENT_SECRET`, `TOSS_API_ACCOUNT_SEQ`, and `TOSS_API_BASE_URL`.
- Implement read methods for accounts, holdings, buying power, sellable quantity, commissions, orders list/detail, prices, stocks, warnings, candles, exchange rate, and KR/US market calendar.
- Implement `scripts/toss_live_smoke.py --preflight` as read-only and default-disabled.
- Add tests for fail-closed config, secret hygiene, host allowlist, token single-flight including dead-port hermetic Redis, decimal parsing, and no broker mutation.

Out of scope:

- No order create/modify/cancel client methods.
- No ledger/reconcile/manual_holdings integration.
- No DB migration.
- No MCP tool registration.
- No data-pipeline wiring.
- No Redis-distributed rate limiter. ROB-530 uses process-local smoothing plus 429 backoff; order-mutation follow-ups can decide whether distributed rate limiting is needed.

## File Structure

- Create: `app/services/brokers/toss/__init__.py`
  - Export only stable public classes and exceptions.
- Create: `app/services/brokers/toss/transport.py`
  - Build `httpx.AsyncClient` with Toss host allowlist, request hook revalidation, and redirect refusal.
- Create: `app/services/brokers/toss/errors.py`
  - Define Toss exception hierarchy and response envelope parsing.
- Create: `app/services/brokers/toss/auth.py`
  - Resolve settings, issue OAuth2 tokens, cache tokens in Redis, and enforce single-flight issuance.
- Create: `app/services/brokers/toss/rate_limiter.py`
  - Implement API-group TPS limits, KST peak-window rules, header feedback, and 429 backoff helpers.
- Create: `app/services/brokers/toss/dto.py`
  - Define dataclass DTOs and decimal parsing helpers for read responses.
- Create: `app/services/brokers/toss/client.py`
  - Provide `TossReadClient` and read-only API methods.
- Modify: `app/core/config.py`
  - Add Toss settings and `validate_toss_api_config()`.
- Create: `scripts/toss_live_smoke.py`
  - Add default-disabled read-only preflight CLI.
- Create: `tests/services/brokers/toss/__init__.py`
- Create: `tests/services/brokers/toss/test_config.py`
- Create: `tests/services/brokers/toss/test_transport.py`
- Create: `tests/services/brokers/toss/test_errors.py`
- Create: `tests/services/brokers/toss/test_auth_single_flight.py`
- Create: `tests/services/brokers/toss/test_rate_limiter.py`
- Create: `tests/services/brokers/toss/test_dto.py`
- Create: `tests/services/brokers/toss/test_client.py`
- Create: `tests/services/brokers/toss/test_smoke_script.py`
- Create: `tests/services/brokers/toss/test_no_mutation_surface.py`

## Decisions To Preserve

- Toss host allowlist is exactly `frozenset({"openapi.tossinvest.com"})`.
- Default base URL is `https://openapi.tossinvest.com`.
- `TOSS_API_CLIENT_SECRET` is `SecretStr | None`; never print `get_secret_value()` except inside token request body.
- Token cache keys include a deterministic non-secret client-id fingerprint so multiple Toss clients do not share a token namespace.
- Token Redis reads/writes fail closed. Unlike KIS `RedisTokenManager`, Toss must not fall back to local token if Redis is unavailable because cross-process token churn invalidates other runtimes.
- `TossReadClient` accepts a test `transport` injection but production construction goes through `from_settings()`.
- Unknown API enum strings and unknown Toss error codes remain strings, not Python enums.
- Decimal fields parse from strings using `Decimal(value)`. Float input raises `TypeError`.
- `get_prices()` and `get_stocks()` enforce 1..200 symbols per request.
- Account header is resolved by `TOSS_API_ACCOUNT_SEQ` if configured; otherwise `GET /accounts` must return exactly one account.

## Task 1: Add Toss Config Gate

**Files:**
- Modify: `app/core/config.py`
- Create: `tests/services/brokers/toss/__init__.py`
- Create: `tests/services/brokers/toss/test_config.py`

- [ ] **Step 1: Write config tests**

Create `tests/services/brokers/toss/__init__.py` as an empty file.

Create `tests/services/brokers/toss/test_config.py`:

```python
from __future__ import annotations

from pydantic import SecretStr

from app.core.config import validate_toss_api_config


class _Settings:
    toss_api_enabled = False
    toss_api_client_id = None
    toss_api_client_secret = None


def test_validate_toss_api_config_disabled_lists_gate_and_credentials() -> None:
    missing = validate_toss_api_config(_Settings())

    assert missing == [
        "TOSS_API_ENABLED",
        "TOSS_API_CLIENT_ID",
        "TOSS_API_CLIENT_SECRET",
    ]


def test_validate_toss_api_config_reports_names_only() -> None:
    class Configured:
        toss_api_enabled = True
        toss_api_client_id = "client-id-value"
        toss_api_client_secret = SecretStr("secret-value")

    assert validate_toss_api_config(Configured()) == []
    assert "secret-value" not in repr(Configured.toss_api_client_secret)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_config.py -q
```

Expected: FAIL with `ImportError` or `NameError` because `validate_toss_api_config` does not exist.

- [ ] **Step 3: Add settings fields and validator**

In `app/core/config.py`, add this near the Kiwoom/KIS broker settings:

```python
    # Toss Securities Open API. Live-only, disabled by default. ROB-530 adds
    # read-only client support; order mutations are handled by follow-up issues.
    toss_api_enabled: bool = False
    toss_api_client_id: str | None = None
    toss_api_client_secret: SecretStr | None = None
    toss_api_account_seq: int | None = None
    toss_api_base_url: str | None = None
```

Add this after `validate_kiwoom_mock_config()`:

```python
def validate_toss_api_config(settings_obj: Any = settings) -> list[str]:
    """Return missing Toss Open API env names without exposing configured values."""

    missing: list[str] = []
    if not bool(getattr(settings_obj, "toss_api_enabled", False)):
        missing.append("TOSS_API_ENABLED")
    if not _has_nonempty_value(getattr(settings_obj, "toss_api_client_id", None)):
        missing.append("TOSS_API_CLIENT_ID")
    if not _has_nonempty_value(getattr(settings_obj, "toss_api_client_secret", None)):
        missing.append("TOSS_API_CLIENT_SECRET")
    return missing
```

- [ ] **Step 4: Verify Task 1**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_config.py -q
uv run ruff check app/core/config.py tests/services/brokers/toss/test_config.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add app/core/config.py tests/services/brokers/toss/__init__.py tests/services/brokers/toss/test_config.py
git commit -m "feat(ROB-530): add Toss API config gate"
```

## Task 2: Host-Guarded Transport

**Files:**
- Create: `app/services/brokers/toss/errors.py`
- Create: `app/services/brokers/toss/transport.py`
- Create: `tests/services/brokers/toss/test_transport.py`

- [ ] **Step 1: Write transport tests**

Create `tests/services/brokers/toss/test_transport.py`:

```python
from __future__ import annotations

import httpx
import pytest

from app.services.brokers.toss.errors import TossHostBlocked
from app.services.brokers.toss.transport import (
    DEFAULT_TOSS_BASE_URL,
    _on_request,
    _on_response,
    build_toss_client,
)


def test_build_toss_client_accepts_default_base_url() -> None:
    client = build_toss_client()
    try:
        assert str(client.base_url) == DEFAULT_TOSS_BASE_URL
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_build_toss_client_rejects_other_host() -> None:
    with pytest.raises(TossHostBlocked):
        build_toss_client(base_url="https://evil.example.com")


def test_build_toss_client_rejects_subdomain_spoof() -> None:
    with pytest.raises(TossHostBlocked):
        build_toss_client(base_url="https://openapi.tossinvest.com.evil.example")


@pytest.mark.asyncio
async def test_on_request_rejects_absolute_url_to_other_host() -> None:
    request = httpx.Request("GET", "https://evil.example.com/api/v1/accounts")

    with pytest.raises(TossHostBlocked):
        await _on_request(request)


@pytest.mark.asyncio
async def test_on_request_accepts_toss_host() -> None:
    request = httpx.Request(
        "GET", "https://openapi.tossinvest.com/api/v1/accounts"
    )

    await _on_request(request)


@pytest.mark.asyncio
async def test_on_response_rejects_redirect() -> None:
    request = httpx.Request(
        "GET", "https://openapi.tossinvest.com/api/v1/accounts"
    )
    response = httpx.Response(
        302,
        headers={"location": "https://evil.example.com/api/v1/accounts"},
        request=request,
    )

    with pytest.raises(TossHostBlocked):
        await _on_response(response)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_transport.py -q
```

Expected: FAIL because `app.services.brokers.toss` modules do not exist.

- [ ] **Step 3: Add error base and transport**

Create `app/services/brokers/toss/errors.py` with the minimal errors needed by this task:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class TossApiErrorBase(RuntimeError):
    """Base class for Toss Open API adapter errors."""


class TossApiDisabled(TossApiErrorBase):
    """Raised when Toss API use is attempted while the env gate is disabled."""


class TossMissingCredentials(TossApiErrorBase):
    """Raised when Toss API credentials are missing."""


class TossHostBlocked(TossApiErrorBase):
    """Raised when a Toss request would leave the allowed Open API host."""


class TossTokenIssuanceUnavailable(TossApiErrorBase):
    """Raised when a contended OAuth issuance never publishes a token."""


@dataclass(frozen=True)
class TossErrorEnvelope:
    request_id: str | None
    code: str
    message: str
    data: dict[str, Any] | None = field(default=None)


class TossApiResponseError(TossApiErrorBase):
    def __init__(self, envelope: TossErrorEnvelope, *, status_code: int) -> None:
        self.envelope = envelope
        self.status_code = status_code
        super().__init__(
            f"Toss API error status={status_code} code={envelope.code!r} "
            f"request_id={envelope.request_id!r}"
        )


class TossRateLimitError(TossApiResponseError):
    """Raised for Toss 429 responses."""
```

Create `app/services/brokers/toss/transport.py`:

```python
from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import httpx

from app.services.brokers.toss.errors import TossHostBlocked

TOSS_API_HOSTS: Final[frozenset[str]] = frozenset({"openapi.tossinvest.com"})
DEFAULT_TOSS_BASE_URL: Final[str] = "https://openapi.tossinvest.com"
DEFAULT_TOSS_TIMEOUT_SECONDS: Final[float] = 10.0


def assert_toss_host(host: str | None) -> None:
    if host not in TOSS_API_HOSTS:
        raise TossHostBlocked(
            f"Host {host!r} is not in TOSS_API_HOSTS. "
            "Allowed: " + ", ".join(sorted(TOSS_API_HOSTS))
        )


def _assert_base_url_is_toss(base_url: str) -> None:
    parsed = urlsplit(base_url)
    assert_toss_host(parsed.hostname)


async def _on_request(request: httpx.Request) -> None:
    assert_toss_host(request.url.host)


async def _on_response(response: httpx.Response) -> None:
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise TossHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Toss Open API endpoints do not legitimately redirect. Refusing."
        )


def build_toss_client(
    *,
    base_url: str = DEFAULT_TOSS_BASE_URL,
    timeout: float = DEFAULT_TOSS_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    _assert_base_url_is_toss(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
```

- [ ] **Step 4: Verify Task 2**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_transport.py -q
uv run ruff check app/services/brokers/toss/errors.py app/services/brokers/toss/transport.py tests/services/brokers/toss/test_transport.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/services/brokers/toss/errors.py app/services/brokers/toss/transport.py tests/services/brokers/toss/test_transport.py
git commit -m "feat(ROB-530): add Toss host-guarded transport"
```

## Task 3: Error Envelope Parser

**Files:**
- Modify: `app/services/brokers/toss/errors.py`
- Create: `tests/services/brokers/toss/test_errors.py`

- [ ] **Step 1: Write error parser tests**

Create `tests/services/brokers/toss/test_errors.py`:

```python
from __future__ import annotations

import httpx
import pytest

from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossErrorEnvelope,
    TossRateLimitError,
    parse_toss_response,
)


def _response(status_code: int, payload: dict, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/accounts")
    return httpx.Response(status_code, json=payload, headers=headers or {}, request=request)


def test_parse_toss_response_returns_result() -> None:
    response = _response(200, {"result": {"accounts": []}})

    assert parse_toss_response(response) == {"accounts": []}


def test_parse_toss_response_allows_message_empty_and_unknown_code() -> None:
    response = _response(
        422,
        {
            "error": {
                "requestId": "req-1",
                "code": "new-unknown-code",
                "message": "",
                "data": {"tickSize": "5", "nearestPrices": ["100", "105"]},
            }
        },
    )

    with pytest.raises(TossApiResponseError) as exc_info:
        parse_toss_response(response)

    envelope = exc_info.value.envelope
    assert envelope == TossErrorEnvelope(
        request_id="req-1",
        code="new-unknown-code",
        message="",
        data={"tickSize": "5", "nearestPrices": ["100", "105"]},
    )
    assert "tickSize" not in str(exc_info.value)


def test_parse_toss_response_429_raises_rate_limit_error() -> None:
    response = _response(
        429,
        {
            "error": {
                "requestId": "req-2",
                "code": "too-many-requests",
                "message": "slow down",
                "data": {"retryAfterSeconds": "1"},
            }
        },
        headers={"Retry-After": "1"},
    )

    with pytest.raises(TossRateLimitError):
        parse_toss_response(response)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_errors.py -q
```

Expected: FAIL because `parse_toss_response` is not implemented.

- [ ] **Step 3: Implement parser**

Append this to `app/services/brokers/toss/errors.py`:

```python
import httpx


def _parse_error_envelope(payload: dict[str, Any]) -> TossErrorEnvelope:
    raw_error = payload.get("error")
    if not isinstance(raw_error, dict):
        return TossErrorEnvelope(
            request_id=None,
            code="malformed-error",
            message="Toss error response did not contain an error object",
            data=None,
        )
    request_id = raw_error.get("requestId")
    code = raw_error.get("code")
    message = raw_error.get("message", "")
    data = raw_error.get("data")
    return TossErrorEnvelope(
        request_id=str(request_id) if request_id is not None else None,
        code=str(code or "unknown-error"),
        message=str(message or ""),
        data=data if isinstance(data, dict) else None,
    )


def parse_toss_response(response: httpx.Response) -> Any:
    payload = response.json()
    if 200 <= response.status_code < 300:
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload
    envelope = _parse_error_envelope(payload if isinstance(payload, dict) else {})
    if response.status_code == 429:
        raise TossRateLimitError(envelope, status_code=response.status_code)
    raise TossApiResponseError(envelope, status_code=response.status_code)
```

Ensure imports at the top remain ordered:

```python
from dataclasses import dataclass, field
from typing import Any

import httpx
```

- [ ] **Step 4: Verify Task 3**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_errors.py -q
uv run ruff check app/services/brokers/toss/errors.py tests/services/brokers/toss/test_errors.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/services/brokers/toss/errors.py tests/services/brokers/toss/test_errors.py
git commit -m "feat(ROB-530): parse Toss response envelopes"
```

## Task 4: Redis Single-Flight OAuth

**Files:**
- Create: `app/services/brokers/toss/auth.py`
- Create: `tests/services/brokers/toss/test_auth_single_flight.py`

- [ ] **Step 1: Write auth tests**

Create `tests/services/brokers/toss/test_auth_single_flight.py`:

```python
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import RedisError

from app.services.brokers.toss import auth
from app.services.brokers.toss.errors import (
    TossMissingCredentials,
    TossTokenIssuanceUnavailable,
)

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool]] = []

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        del ex
        self.set_calls.append((key, value, nx))
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.strings:
                self.strings.pop(key, None)
                removed += 1
        return removed

    async def eval(self, script: str, key_count: int, key: str, token: str) -> int:
        del script, key_count
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0


@dataclass
class _Settings:
    toss_api_enabled: bool = True
    toss_api_client_id: str | None = "client-id"
    toss_api_client_secret: SecretStr | None = SecretStr("client-secret")
    toss_api_base_url: str | None = "https://openapi.tossinvest.com"


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _get_client():
        return redis

    monkeypatch.setattr(auth, "_get_redis_client", _get_client)
    monkeypatch.setattr(auth, "TOKEN_WAIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(auth, "TOKEN_WAIT_POLL_SECONDS", 0.01)
    return redis


def test_from_settings_fails_closed_missing_gate() -> None:
    settings = _Settings(toss_api_enabled=False)

    with pytest.raises(TossMissingCredentials) as exc_info:
        auth.TossOAuthTokenManager.from_settings(settings)

    assert "TOSS_API_ENABLED" in str(exc_info.value)
    assert "client-secret" not in str(exc_info.value)


async def test_concurrent_cold_start_issues_exactly_once(fake_redis, monkeypatch):
    issue_calls = 0
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        await asyncio.sleep(0.05)
        return auth.TossToken(access_token="issued-token", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    results = await asyncio.gather(*(manager.get_access_token() for _ in range(10)))

    assert results == ["issued-token"] * 10
    assert issue_calls == 1


async def test_contender_times_out_without_independent_issue(fake_redis, monkeypatch):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.lock_key] = "other-owner"
    issue_calls = 0

    async def _issue() -> auth.TossToken:
        nonlocal issue_calls
        issue_calls += 1
        return auth.TossToken(access_token="must-not-issue", expires_in=86399)

    monkeypatch.setattr(manager, "_issue_token", _issue)

    with pytest.raises(TossTokenIssuanceUnavailable):
        await manager.get_access_token()

    assert issue_calls == 0


async def test_dead_port_redis_is_not_masked(monkeypatch):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )

    async def _broken_client():
        raise RedisError("Connection refused on dead test port")

    monkeypatch.setattr(auth, "_get_redis_client", _broken_client)

    with pytest.raises(RedisError, match="dead test port"):
        await manager.get_access_token()


async def test_cached_token_is_reused(fake_redis):
    manager = auth.TossOAuthTokenManager(
        client_id="client-id",
        client_secret=SecretStr("client-secret"),
        base_url="https://openapi.tossinvest.com",
    )
    fake_redis.strings[manager.token_key] = json.dumps(
        {"access_token": "cached-token", "expires_at": 4_102_444_800.0}
    )

    assert await manager.get_access_token() == "cached-token"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_auth_single_flight.py -q
```

Expected: FAIL because `auth.py` is not implemented.

- [ ] **Step 3: Implement auth manager**

Create `app/services/brokers/toss/auth.py`:

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx
import redis.asyncio as redis
from pydantic import SecretStr

from app.core.config import settings, validate_toss_api_config
from app.services.brokers.toss.errors import (
    TossMissingCredentials,
    TossTokenIssuanceUnavailable,
)
from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL, build_toss_client

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_BUFFER_SECONDS: Final[int] = 120
TOKEN_LOCK_TTL_SECONDS: Final[int] = 30
TOKEN_WAIT_TIMEOUT_SECONDS: float = 5.0
TOKEN_WAIT_POLL_SECONDS: float = 0.05

_redis_client: redis.Redis | None = None


@dataclass(frozen=True)
class TossToken:
    access_token: str
    expires_in: int


async def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        redis_url = settings.get_redis_url()
        _redis_client = redis.from_url(
            redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            decode_responses=True,
        )
    return _redis_client


async def close_toss_token_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None


def _client_fingerprint(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


class TossOAuthTokenManager:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        base_url: str = DEFAULT_TOSS_BASE_URL,
    ) -> None:
        if not client_id.strip():
            raise TossMissingCredentials("TOSS_API_CLIENT_ID is empty")
        secret_value = client_secret.get_secret_value()
        if not secret_value.strip():
            raise TossMissingCredentials("TOSS_API_CLIENT_SECRET is empty")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url
        self._namespace = f"toss:oauth:{_client_fingerprint(client_id)}"
        self.token_key = f"{self._namespace}:access_token"
        self.lock_key = f"{self._namespace}:lock"

    def __repr__(self) -> str:
        return (
            f"<TossOAuthTokenManager base_url={self._base_url!r} "
            f"client_id_fp={_client_fingerprint(self._client_id)!r}>"
        )

    @classmethod
    def from_settings(cls, settings_obj: Any = settings) -> "TossOAuthTokenManager":
        missing = validate_toss_api_config(settings_obj)
        if missing:
            raise TossMissingCredentials(
                "Toss API is disabled or missing required configuration: "
                + ", ".join(missing)
            )
        secret = getattr(settings_obj, "toss_api_client_secret")
        if not isinstance(secret, SecretStr):
            secret = SecretStr(str(secret))
        base_url = getattr(settings_obj, "toss_api_base_url", None) or DEFAULT_TOSS_BASE_URL
        return cls(
            client_id=str(getattr(settings_obj, "toss_api_client_id")),
            client_secret=secret,
            base_url=str(base_url),
        )

    async def get_access_token(self, *, force_reissue: bool = False) -> str:
        if not force_reissue:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
        return await self._issue_single_flight(force_reissue=force_reissue)

    async def _get_cached_token(self) -> str | None:
        redis_client = await _get_redis_client()
        raw = await redis_client.get(self.token_key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            access_token = data["access_token"]
            expires_at = float(data["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() >= expires_at - TOKEN_EXPIRY_BUFFER_SECONDS:
            return None
        return str(access_token)

    async def _cache_token(self, token: TossToken) -> None:
        now = time.time()
        expires_at = now + int(token.expires_in)
        payload = {"access_token": token.access_token, "expires_at": expires_at}
        redis_client = await _get_redis_client()
        ttl = max(int(token.expires_in), 1)
        await redis_client.set(self.token_key, json.dumps(payload), ex=ttl)

    async def _issue_single_flight(self, *, force_reissue: bool = False) -> str:
        redis_client = await _get_redis_client()
        lock_token = str(uuid.uuid4())
        acquired = await redis_client.set(
            self.lock_key,
            lock_token,
            nx=True,
            ex=TOKEN_LOCK_TTL_SECONDS,
        )
        if acquired:
            try:
                if force_reissue:
                    await redis_client.delete(self.token_key)
                else:
                    cached = await self._get_cached_token()
                    if cached is not None:
                        return cached
                issued = await self._issue_token()
                await self._cache_token(issued)
                logger.info("Toss OAuth token issued and cached")
                return issued.access_token
            finally:
                await self._release_lock(redis_client, lock_token)
        waited = await self._wait_for_cached_token()
        if waited is not None:
            return waited
        raise TossTokenIssuanceUnavailable(
            "Toss OAuth token issuance contended; no cached token after bounded wait"
        )

    async def _wait_for_cached_token(self) -> str | None:
        deadline = time.monotonic() + TOKEN_WAIT_TIMEOUT_SECONDS
        while True:
            cached = await self._get_cached_token()
            if cached is not None:
                return cached
            if time.monotonic() >= deadline:
                return None
            poll = max(float(TOKEN_WAIT_POLL_SECONDS), 0.0)
            await asyncio.sleep(poll + random.uniform(0.0, poll))

    async def _release_lock(self, redis_client: redis.Redis, lock_token: str) -> None:
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            await redis_client.eval(script, 1, self.lock_key, lock_token)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Toss OAuth lock release best-effort failure: %s", exc)

    async def _issue_token(self) -> TossToken:
        async with build_toss_client(base_url=self._base_url) as client:
            response = await client.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        response.raise_for_status()
        payload = response.json()
        return TossToken(
            access_token=str(payload["access_token"]),
            expires_in=int(payload["expires_in"]),
        )
```

- [ ] **Step 4: Verify Task 4**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_auth_single_flight.py -q
uv run ruff check app/services/brokers/toss/auth.py tests/services/brokers/toss/test_auth_single_flight.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add app/services/brokers/toss/auth.py tests/services/brokers/toss/test_auth_single_flight.py
git commit -m "feat(ROB-530): add Toss OAuth single-flight token manager"
```

## Task 5: Rate Limiter And Retry Policy

**Files:**
- Create: `app/services/brokers/toss/rate_limiter.py`
- Create: `tests/services/brokers/toss/test_rate_limiter.py`

- [ ] **Step 1: Write rate limiter tests**

Create `tests/services/brokers/toss/test_rate_limiter.py`:

```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.brokers.toss.rate_limiter import (
    TossApiGroup,
    TossRateLimiter,
    retry_delay_seconds,
)


def test_order_info_peak_limit_is_three_tps() -> None:
    now = datetime(2026, 6, 12, 9, 5, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 3


def test_order_info_normal_limit_is_six_tps() -> None:
    now = datetime(2026, 6, 12, 9, 11, tzinfo=ZoneInfo("Asia/Seoul"))

    assert TossRateLimiter.limit_for(TossApiGroup.ORDER_INFO, now=now) == 6


def test_market_data_limit_is_ten_tps() -> None:
    limiter = TossRateLimiter()

    assert limiter.limit_for(TossApiGroup.MARKET_DATA) == 10


@pytest.mark.parametrize(
    ("retry_after", "attempt", "expected_min"),
    [("2", 0, 2.0), (None, 2, 4.0), ("bad", 1, 2.0)],
)
def test_retry_delay_seconds_uses_header_or_backoff(
    retry_after: str | None, attempt: int, expected_min: float
) -> None:
    delay = retry_delay_seconds(retry_after, attempt=attempt, jitter=0.0)

    assert delay == expected_min
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_rate_limiter.py -q
```

Expected: FAIL because `rate_limiter.py` does not exist.

- [ ] **Step 3: Implement rate limiter**

Create `app/services/brokers/toss/rate_limiter.py`:

```python
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo


class TossApiGroup(StrEnum):
    AUTH = "AUTH"
    ACCOUNT = "ACCOUNT"
    ASSET = "ASSET"
    STOCK = "STOCK"
    MARKET_INFO = "MARKET_INFO"
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"
    ORDER = "ORDER"
    ORDER_HISTORY = "ORDER_HISTORY"
    ORDER_INFO = "ORDER_INFO"


_BASE_LIMITS: dict[TossApiGroup, int] = {
    TossApiGroup.AUTH: 5,
    TossApiGroup.ACCOUNT: 1,
    TossApiGroup.ASSET: 5,
    TossApiGroup.STOCK: 5,
    TossApiGroup.MARKET_INFO: 3,
    TossApiGroup.MARKET_DATA: 10,
    TossApiGroup.MARKET_DATA_CHART: 5,
    TossApiGroup.ORDER: 6,
    TossApiGroup.ORDER_HISTORY: 5,
    TossApiGroup.ORDER_INFO: 6,
}


class TossRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[TossApiGroup, deque[float]] = {
            group: deque() for group in TossApiGroup
        }
        self._lock = asyncio.Lock()

    @staticmethod
    def limit_for(
        group: TossApiGroup, *, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(ZoneInfo("Asia/Seoul"))
        if group in {TossApiGroup.ORDER, TossApiGroup.ORDER_INFO}:
            if now.hour == 9 and 0 <= now.minute < 10:
                return 3
        return _BASE_LIMITS[group]

    async def acquire(self, group: TossApiGroup) -> None:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[group]
            while bucket and now - bucket[0] >= 1.0:
                bucket.popleft()
            limit = self.limit_for(group)
            if len(bucket) >= limit:
                sleep_for = max(1.0 - (now - bucket[0]), 0.0)
                await asyncio.sleep(sleep_for)
                now = time.monotonic()
                while bucket and now - bucket[0] >= 1.0:
                    bucket.popleft()
            bucket.append(now)


def retry_delay_seconds(
    retry_after: str | None, *, attempt: int, jitter: float | None = None
) -> float:
    try:
        if retry_after is not None:
            return max(float(retry_after), 0.0)
    except ValueError:
        pass
    base = min(2.0**attempt, 16.0)
    return base + (random.uniform(0.0, base) if jitter is None else jitter)
```

- [ ] **Step 4: Verify Task 5**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_rate_limiter.py -q
uv run ruff check app/services/brokers/toss/rate_limiter.py tests/services/brokers/toss/test_rate_limiter.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add app/services/brokers/toss/rate_limiter.py tests/services/brokers/toss/test_rate_limiter.py
git commit -m "feat(ROB-530): add Toss rate limiter policy"
```

## Task 6: DTOs And Decimal Parsing

**Files:**
- Create: `app/services/brokers/toss/dto.py`
- Create: `tests/services/brokers/toss/test_dto.py`

- [ ] **Step 1: Write DTO tests**

Create `tests/services/brokers/toss/test_dto.py`:

```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import (
    parse_decimal_string,
    parse_holdings,
    parse_orders,
    parse_prices,
    parse_stocks,
)


def test_parse_decimal_string_rejects_float() -> None:
    with pytest.raises(TypeError, match="float"):
        parse_decimal_string(1.23)


def test_parse_prices_converts_decimal_strings() -> None:
    prices = parse_prices(
        [
            {
                "symbol": "BRK.B",
                "timestamp": "2026-06-12T00:00:00Z",
                "lastPrice": "430.12",
                "currency": "USD",
            }
        ]
    )

    assert prices[0].symbol == "BRK.B"
    assert prices[0].last_price == Decimal("430.12")
    assert prices[0].currency == "USD"


def test_parse_stocks_preserves_unknown_enum_strings() -> None:
    stocks = parse_stocks(
        [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "englishName": "Samsung Electronics",
                "isinCode": "KR7005930003",
                "market": "UNKNOWN_MARKET",
                "securityType": "NEW_TYPE",
                "isCommonShare": True,
                "status": "ACTIVE",
                "currency": "KRW",
                "listDate": "1975-06-11",
                "delistDate": None,
                "sharesOutstanding": "5841240000",
                "leverageFactor": None,
                "koreanMarketDetail": {"nxtSupported": True},
            }
        ]
    )

    assert stocks[0].security_type == "NEW_TYPE"
    assert stocks[0].shares_outstanding == Decimal("5841240000")


def test_parse_holdings_converts_nested_decimal_strings() -> None:
    holdings = parse_holdings(
        {
            "items": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "10",
                    "lastPrice": "70000",
                    "averagePurchasePrice": "65000",
                    "marketValue": {
                        "purchaseAmount": "650000",
                        "amount": "700000",
                        "amountAfterCost": "699000",
                    },
                    "profitLoss": {"amount": "50000", "rate": "0.0769"},
                    "dailyProfitLoss": {"amount": "1000", "rate": "0.0014"},
                    "cost": {"commission": "0", "tax": "0"},
                }
            ]
        }
    )

    assert holdings.items[0].quantity == Decimal("10")
    assert holdings.items[0].market_value["amount"] == Decimal("700000")


def test_parse_orders_converts_execution_decimals() -> None:
    orders = parse_orders(
        {
            "orders": [
                {
                    "orderId": "ord-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FUTURE_STATUS",
                    "price": "190.00",
                    "quantity": "1.5",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-12T00:00:00Z",
                    "canceledAt": None,
                    "execution": {
                        "filledQuantity": "0.5",
                        "averageFilledPrice": "189.50",
                        "filledAmount": "94.75",
                        "commission": "0.10",
                        "tax": None,
                        "filledAt": "2026-06-12T00:01:00Z",
                        "settlementDate": "2026-06-14",
                    },
                }
            ],
            "nextCursor": None,
            "hasNext": False,
        }
    )

    assert orders.orders[0].status == "FUTURE_STATUS"
    assert orders.orders[0].execution["commission"] == Decimal("0.10")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_dto.py -q
```

Expected: FAIL because `dto.py` does not exist.

- [ ] **Step 3: Implement DTOs**

Create `app/services/brokers/toss/dto.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def parse_decimal_string(value: object) -> Decimal:
    if isinstance(value, float):
        raise TypeError("Toss decimal values must be strings, not float")
    if value is None:
        raise TypeError("Toss decimal value is required")
    return Decimal(str(value))


def parse_optional_decimal_string(value: object) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal_string(value)


def _decimal_map(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: parse_optional_decimal_string(value)
        if value is None or isinstance(value, str | int | float)
        else value
        for key, value in raw.items()
    }


@dataclass(frozen=True)
class TossAccount:
    account_no: str
    account_seq: int
    account_type: str


@dataclass(frozen=True)
class TossPrice:
    symbol: str
    timestamp: str | None
    last_price: Decimal
    currency: str


@dataclass(frozen=True)
class TossStockInfo:
    symbol: str
    name: str
    english_name: str
    isin_code: str
    market: str
    security_type: str
    is_common_share: bool
    status: str
    currency: str
    list_date: str | None
    delist_date: str | None
    shares_outstanding: Decimal
    leverage_factor: Decimal | None
    korean_market_detail: dict[str, Any] | None


@dataclass(frozen=True)
class TossHoldingItem:
    symbol: str
    name: str
    market_country: str
    currency: str
    quantity: Decimal
    last_price: Decimal
    average_purchase_price: Decimal
    market_value: dict[str, Any]
    profit_loss: dict[str, Any]
    daily_profit_loss: dict[str, Any]
    cost: dict[str, Any]


@dataclass(frozen=True)
class TossHoldings:
    items: list[TossHoldingItem] = field(default_factory=list)
    raw_overview: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TossOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    price: Decimal | None
    quantity: Decimal
    order_amount: Decimal | None
    currency: str
    ordered_at: str
    canceled_at: str | None
    execution: dict[str, Any]


@dataclass(frozen=True)
class TossOrdersPage:
    orders: list[TossOrder]
    next_cursor: str | None
    has_next: bool


def parse_accounts(raw: list[dict[str, Any]]) -> list[TossAccount]:
    return [
        TossAccount(
            account_no=str(row["accountNo"]),
            account_seq=int(row["accountSeq"]),
            account_type=str(row["accountType"]),
        )
        for row in raw
    ]


def parse_prices(raw: list[dict[str, Any]]) -> list[TossPrice]:
    return [
        TossPrice(
            symbol=str(row["symbol"]),
            timestamp=row.get("timestamp"),
            last_price=parse_decimal_string(row["lastPrice"]),
            currency=str(row["currency"]),
        )
        for row in raw
    ]


def parse_stocks(raw: list[dict[str, Any]]) -> list[TossStockInfo]:
    return [
        TossStockInfo(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            english_name=str(row["englishName"]),
            isin_code=str(row["isinCode"]),
            market=str(row["market"]),
            security_type=str(row["securityType"]),
            is_common_share=bool(row["isCommonShare"]),
            status=str(row["status"]),
            currency=str(row["currency"]),
            list_date=row.get("listDate"),
            delist_date=row.get("delistDate"),
            shares_outstanding=parse_decimal_string(row["sharesOutstanding"]),
            leverage_factor=parse_optional_decimal_string(row.get("leverageFactor")),
            korean_market_detail=row.get("koreanMarketDetail"),
        )
        for row in raw
    ]


def parse_holdings(raw: dict[str, Any]) -> TossHoldings:
    items = []
    for row in raw.get("items", []):
        items.append(
            TossHoldingItem(
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                market_country=str(row["marketCountry"]),
                currency=str(row["currency"]),
                quantity=parse_decimal_string(row["quantity"]),
                last_price=parse_decimal_string(row["lastPrice"]),
                average_purchase_price=parse_decimal_string(
                    row["averagePurchasePrice"]
                ),
                market_value=_decimal_map(dict(row["marketValue"])),
                profit_loss=_decimal_map(dict(row["profitLoss"])),
                daily_profit_loss=_decimal_map(dict(row["dailyProfitLoss"])),
                cost=_decimal_map(dict(row["cost"])),
            )
        )
    overview = {key: value for key, value in raw.items() if key != "items"}
    return TossHoldings(items=items, raw_overview=overview)


def _parse_execution(raw: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(raw)
    for key in ("filledQuantity", "averageFilledPrice", "filledAmount", "commission", "tax"):
        if key in parsed:
            parsed[key] = parse_optional_decimal_string(parsed[key])
    return parsed


def parse_orders(raw: dict[str, Any]) -> TossOrdersPage:
    orders = []
    for row in raw.get("orders", []):
        orders.append(
            TossOrder(
                order_id=str(row["orderId"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                order_type=str(row["orderType"]),
                time_in_force=str(row["timeInForce"]),
                status=str(row["status"]),
                price=parse_optional_decimal_string(row.get("price")),
                quantity=parse_decimal_string(row["quantity"]),
                order_amount=parse_optional_decimal_string(row.get("orderAmount")),
                currency=str(row["currency"]),
                ordered_at=str(row["orderedAt"]),
                canceled_at=row.get("canceledAt"),
                execution=_parse_execution(dict(row.get("execution") or {})),
            )
        )
    return TossOrdersPage(
        orders=orders,
        next_cursor=raw.get("nextCursor"),
        has_next=bool(raw.get("hasNext", False)),
    )
```

- [ ] **Step 4: Verify Task 6**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_dto.py -q
uv run ruff check app/services/brokers/toss/dto.py tests/services/brokers/toss/test_dto.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 6**

```bash
git add app/services/brokers/toss/dto.py tests/services/brokers/toss/test_dto.py
git commit -m "feat(ROB-530): add Toss read DTO parsers"
```

## Task 7: Read Client

**Files:**
- Create: `app/services/brokers/toss/client.py`
- Modify: `app/services/brokers/toss/dto.py`
- Create: `tests/services/brokers/toss/test_client.py`

- [ ] **Step 1: Write client tests**

Create `tests/services/brokers/toss/test_client.py`:

```python
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.client import TossReadClient


class _TokenManager(TossOAuthTokenManager):
    def __init__(self) -> None:
        pass

    async def get_access_token(self, *, force_reissue: bool = False) -> str:
        del force_reissue
        return "token-1"


def _json(payload):
    return {"result": payload}


@pytest.mark.asyncio
async def test_prices_sends_comma_symbols_and_authorization() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["symbols"] = request.url.params["symbols"]
        return httpx.Response(
            200,
            json=_json(
                [
                    {
                        "symbol": "AAPL",
                        "timestamp": "2026-06-12T00:00:00Z",
                        "lastPrice": "190.12",
                        "currency": "USD",
                    }
                ]
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        prices = await client.prices(["AAPL", "BRK.B"])
    finally:
        await client.aclose()

    assert seen == {"authorization": "Bearer token-1", "symbols": "AAPL,BRK.B"}
    assert prices[0].last_price == Decimal("190.12")


@pytest.mark.asyncio
async def test_holdings_auto_resolves_single_account_header() -> None:
    seen_headers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json=_json(
                    [{"accountNo": "12345678", "accountSeq": 1, "accountType": "BROKERAGE"}]
                ),
                request=request,
            )
        seen_headers.append(request.headers["X-Tossinvest-Account"])
        return httpx.Response(200, json=_json({"items": []}), request=request)

    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(handler),
    )
    try:
        holdings = await client.holdings()
    finally:
        await client.aclose()

    assert seen_headers == ["1"]
    assert holdings.items == []


@pytest.mark.asyncio
async def test_prices_rejects_more_than_200_symbols() -> None:
    client = TossReadClient(
        token_manager=_TokenManager(),
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
    )
    try:
        with pytest.raises(ValueError, match="1..200"):
            await client.prices([f"S{i}" for i in range(201)])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_order_retries_once_after_invalid_token() -> None:
    calls = 0
    token_calls: list[bool] = []

    class TokenManager(_TokenManager):
        async def get_access_token(self, *, force_reissue: bool = False) -> str:
            token_calls.append(force_reissue)
            return "token-2" if force_reissue else "token-1"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "requestId": "req",
                        "code": "invalid-token",
                        "message": "",
                        "data": None,
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json=_json(
                {
                    "orderId": "ord-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FILLED",
                    "price": "190",
                    "quantity": "1",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-12T00:00:00Z",
                    "canceledAt": None,
                    "execution": {"filledQuantity": "1"},
                }
            ),
            request=request,
        )

    client = TossReadClient(
        token_manager=TokenManager(),
        account_seq=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        order = await client.get_order("ord-1")
    finally:
        await client.aclose()

    assert order.order_id == "ord-1"
    assert token_calls == [False, True]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py -q
```

Expected: FAIL because `client.py` is not implemented.

- [ ] **Step 3: Add missing DTO parsers**

Append to `app/services/brokers/toss/dto.py`:

```python
@dataclass(frozen=True)
class TossBuyingPower:
    currency: str
    cash_buying_power: Decimal


@dataclass(frozen=True)
class TossSellableQuantity:
    sellable_quantity: Decimal


@dataclass(frozen=True)
class TossCommission:
    market_country: str
    commission_rate: Decimal
    start_date: str | None
    end_date: str | None


def parse_buying_power(raw: dict[str, Any]) -> TossBuyingPower:
    return TossBuyingPower(
        currency=str(raw["currency"]),
        cash_buying_power=parse_decimal_string(raw["cashBuyingPower"]),
    )


def parse_sellable_quantity(raw: dict[str, Any]) -> TossSellableQuantity:
    return TossSellableQuantity(
        sellable_quantity=parse_decimal_string(raw["sellableQuantity"])
    )


def parse_commissions(raw: list[dict[str, Any]]) -> list[TossCommission]:
    return [
        TossCommission(
            market_country=str(row["marketCountry"]),
            commission_rate=parse_decimal_string(row["commissionRate"]),
            start_date=row.get("startDate"),
            end_date=row.get("endDate"),
        )
        for row in raw
    ]


def parse_order(raw: dict[str, Any]) -> TossOrder:
    return parse_orders({"orders": [raw], "nextCursor": None, "hasNext": False}).orders[0]
```

- [ ] **Step 4: Implement read client**

Create `app/services/brokers/toss/client.py`:

```python
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.services.brokers.toss.auth import TossOAuthTokenManager
from app.services.brokers.toss.dto import (
    TossAccount,
    parse_accounts,
    parse_buying_power,
    parse_commissions,
    parse_holdings,
    parse_order,
    parse_orders,
    parse_prices,
    parse_sellable_quantity,
    parse_stocks,
)
from app.services.brokers.toss.errors import TossApiResponseError, parse_toss_response
from app.services.brokers.toss.rate_limiter import TossApiGroup, TossRateLimiter
from app.services.brokers.toss.transport import DEFAULT_TOSS_BASE_URL, build_toss_client


_TOKEN_CODES = {"invalid-token", "expired-token"}


class TossReadClient:
    def __init__(
        self,
        *,
        token_manager: TossOAuthTokenManager,
        account_seq: int | None = None,
        base_url: str = DEFAULT_TOSS_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: TossRateLimiter | None = None,
    ) -> None:
        self._token_manager = token_manager
        self._account_seq = account_seq
        self._client = build_toss_client(base_url=base_url, transport=transport)
        self._rate_limiter = rate_limiter or TossRateLimiter()

    @classmethod
    def from_settings(cls, settings_obj: Any = settings) -> "TossReadClient":
        base_url = getattr(settings_obj, "toss_api_base_url", None) or DEFAULT_TOSS_BASE_URL
        return cls(
            token_manager=TossOAuthTokenManager.from_settings(settings_obj),
            account_seq=getattr(settings_obj, "toss_api_account_seq", None),
            base_url=str(base_url),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        group: TossApiGroup,
        params: dict[str, Any] | None = None,
        account_required: bool = False,
    ) -> Any:
        await self._rate_limiter.acquire(group)
        token = await self._token_manager.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_required:
            headers["X-Tossinvest-Account"] = str(await self._resolve_account_seq())
        response = await self._client.request(method, path, params=params, headers=headers)
        try:
            return parse_toss_response(response)
        except TossApiResponseError as exc:
            if exc.envelope.code in _TOKEN_CODES:
                token = await self._token_manager.get_access_token(force_reissue=True)
                headers["Authorization"] = f"Bearer {token}"
                retry = await self._client.request(
                    method, path, params=params, headers=headers
                )
                return parse_toss_response(retry)
            raise

    async def _resolve_account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        accounts = await self.accounts()
        if len(accounts) != 1:
            raise ValueError(
                f"Toss account auto-resolution requires exactly one account; got {len(accounts)}"
            )
        self._account_seq = accounts[0].account_seq
        return self._account_seq

    @staticmethod
    def _symbols_param(symbols: list[str] | tuple[str, ...]) -> str:
        if not 1 <= len(symbols) <= 200:
            raise ValueError("Toss symbol batch size must be 1..200")
        return ",".join(symbols)

    async def accounts(self) -> list[TossAccount]:
        return parse_accounts(
            await self._request("GET", "/api/v1/accounts", group=TossApiGroup.ACCOUNT)
        )

    async def holdings(self, *, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else None
        return parse_holdings(
            await self._request(
                "GET",
                "/api/v1/holdings",
                group=TossApiGroup.ASSET,
                params=params,
                account_required=True,
            )
        )

    async def prices(self, symbols: list[str] | tuple[str, ...]):
        return parse_prices(
            await self._request(
                "GET",
                "/api/v1/prices",
                group=TossApiGroup.MARKET_DATA,
                params={"symbols": self._symbols_param(symbols)},
            )
        )

    async def stocks(self, symbols: list[str] | tuple[str, ...]):
        return parse_stocks(
            await self._request(
                "GET",
                "/api/v1/stocks",
                group=TossApiGroup.STOCK,
                params={"symbols": self._symbols_param(symbols)},
            )
        )

    async def warnings(self, symbol: str) -> Any:
        return await self._request(
            "GET",
            f"/api/v1/stocks/{symbol}/warnings",
            group=TossApiGroup.STOCK,
        )

    async def candles(
        self,
        symbol: str,
        *,
        interval: str,
        count: int | None = None,
        before: str | None = None,
        adjusted: bool | None = None,
    ) -> Any:
        if interval not in {"1m", "1d"}:
            raise ValueError("Toss candle interval must be '1m' or '1d'")
        params = {
            key: value
            for key, value in {
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "before": before,
                "adjusted": adjusted,
            }.items()
            if value is not None
        }
        return await self._request(
            "GET",
            "/api/v1/candles",
            group=TossApiGroup.MARKET_DATA_CHART,
            params=params,
        )

    async def exchange_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        date_time: str | None = None,
    ) -> Any:
        params = {
            "baseCurrency": base_currency,
            "quoteCurrency": quote_currency,
        }
        if date_time is not None:
            params["dateTime"] = date_time
        return await self._request(
            "GET",
            "/api/v1/exchange-rate",
            group=TossApiGroup.MARKET_INFO,
            params=params,
        )

    async def market_calendar_kr(self, *, date: str | None = None) -> Any:
        return await self._request(
            "GET",
            "/api/v1/market-calendar/KR",
            group=TossApiGroup.MARKET_INFO,
            params={"date": date} if date else None,
        )

    async def market_calendar_us(self, *, date: str | None = None) -> Any:
        return await self._request(
            "GET",
            "/api/v1/market-calendar/US",
            group=TossApiGroup.MARKET_INFO,
            params={"date": date} if date else None,
        )

    async def list_orders(
        self,
        *,
        status: str,
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ):
        params = {
            key: value
            for key, value in {
                "status": status,
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "cursor": cursor,
                "limit": limit,
            }.items()
            if value is not None
        }
        return parse_orders(
            await self._request(
                "GET",
                "/api/v1/orders",
                group=TossApiGroup.ORDER_HISTORY,
                params=params,
                account_required=True,
            )
        )

    async def get_order(self, order_id: str):
        return parse_order(
            await self._request(
                "GET",
                f"/api/v1/orders/{order_id}",
                group=TossApiGroup.ORDER_HISTORY,
                account_required=True,
            )
        )

    async def buying_power(self, *, currency: str):
        return parse_buying_power(
            await self._request(
                "GET",
                "/api/v1/buying-power",
                group=TossApiGroup.ORDER_INFO,
                params={"currency": currency},
                account_required=True,
            )
        )

    async def sellable_quantity(self, *, symbol: str):
        return parse_sellable_quantity(
            await self._request(
                "GET",
                "/api/v1/sellable-quantity",
                group=TossApiGroup.ORDER_INFO,
                params={"symbol": symbol},
                account_required=True,
            )
        )

    async def commissions(self):
        return parse_commissions(
            await self._request(
                "GET",
                "/api/v1/commissions",
                group=TossApiGroup.ORDER_INFO,
                account_required=True,
            )
        )
```

- [ ] **Step 5: Verify Task 7**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py -q
uv run ruff check app/services/brokers/toss/client.py app/services/brokers/toss/dto.py tests/services/brokers/toss/test_client.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add app/services/brokers/toss/client.py app/services/brokers/toss/dto.py tests/services/brokers/toss/test_client.py
git commit -m "feat(ROB-530): add Toss read client"
```

## Task 8: Package Exports And Secret Hygiene

**Files:**
- Create: `app/services/brokers/toss/__init__.py`
- Modify: `tests/services/brokers/toss/test_auth_single_flight.py`
- Create: `tests/services/brokers/toss/test_secret_redaction.py`

- [ ] **Step 1: Add export and secret tests**

Create `tests/services/brokers/toss/test_secret_redaction.py`:

```python
from __future__ import annotations

from pydantic import SecretStr

from app.services.brokers.toss.auth import TossOAuthTokenManager


def test_token_manager_repr_does_not_leak_secret_or_raw_client_id() -> None:
    manager = TossOAuthTokenManager(
        client_id="ROB530_CLIENT_ID_SHOULD_NOT_LEAK",
        client_secret=SecretStr("ROB530_CLIENT_SECRET_SHOULD_NOT_LEAK"),
        base_url="https://openapi.tossinvest.com",
    )

    rep = repr(manager)

    assert "ROB530_CLIENT_SECRET_SHOULD_NOT_LEAK" not in rep
    assert "ROB530_CLIENT_ID_SHOULD_NOT_LEAK" not in rep
    assert "client_id_fp" in rep
```

Create `app/services/brokers/toss/__init__.py`:

```python
from __future__ import annotations

from app.services.brokers.toss.auth import TossOAuthTokenManager, close_toss_token_redis
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.errors import (
    TossApiDisabled,
    TossApiErrorBase,
    TossApiResponseError,
    TossHostBlocked,
    TossMissingCredentials,
    TossRateLimitError,
    TossTokenIssuanceUnavailable,
)

__all__ = [
    "TossApiDisabled",
    "TossApiErrorBase",
    "TossApiResponseError",
    "TossHostBlocked",
    "TossMissingCredentials",
    "TossOAuthTokenManager",
    "TossRateLimitError",
    "TossReadClient",
    "TossTokenIssuanceUnavailable",
    "close_toss_token_redis",
]
```

- [ ] **Step 2: Run tests and verify**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_secret_redaction.py tests/services/brokers/toss/test_auth_single_flight.py -q
uv run ruff check app/services/brokers/toss/__init__.py tests/services/brokers/toss/test_secret_redaction.py
```

Expected: PASS.

- [ ] **Step 3: Commit Task 8**

```bash
git add app/services/brokers/toss/__init__.py tests/services/brokers/toss/test_secret_redaction.py tests/services/brokers/toss/test_auth_single_flight.py
git commit -m "feat(ROB-530): export Toss client surface safely"
```

## Task 9: Disabled-By-Default Preflight Smoke

**Files:**
- Create: `scripts/toss_live_smoke.py`
- Create: `tests/services/brokers/toss/test_smoke_script.py`

- [ ] **Step 1: Write smoke script tests**

Create `tests/services/brokers/toss/test_smoke_script.py`:

```python
from __future__ import annotations

import pytest

from scripts import toss_live_smoke


def test_main_without_preflight_exits_zero(capsys) -> None:
    code = toss_live_smoke.main([])

    assert code == 0
    assert "disabled" in capsys.readouterr().out


def test_main_disabled_env_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(["--preflight"])

    assert code == 0
    assert "TOSS_API_ENABLED" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_preflight_redacts_secret(monkeypatch, capsys) -> None:
    class FakeClient:
        async def accounts(self):
            return [type("Account", (), {"account_seq": 1})()]

        async def holdings(self):
            return type("Holdings", (), {"items": []})()

        async def prices(self, symbols):
            assert symbols == ["005930"]
            return []

        async def aclose(self):
            return None

    monkeypatch.setattr(toss_live_smoke.TossReadClient, "from_settings", lambda: FakeClient())

    code = await toss_live_smoke.run_preflight(["005930"])

    assert code == 0
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py -q
```

Expected: FAIL because `scripts/toss_live_smoke.py` does not exist.

- [ ] **Step 3: Implement smoke script**

Create `scripts/toss_live_smoke.py`:

```python
from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from app.services.brokers.toss.client import TossReadClient


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


async def run_preflight(symbols: Sequence[str]) -> int:
    client = TossReadClient.from_settings()
    try:
        accounts = await client.accounts()
        holdings = await client.holdings()
        prices = await client.prices(list(symbols))
    finally:
        await client.aclose()
    print(
        "Toss preflight ok: "
        f"accounts={len(accounts)} holdings={len(holdings.items)} prices={len(prices)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toss Open API read-only smoke")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--symbol", action="append", default=["005930"])
    args = parser.parse_args(argv)

    if not args.preflight:
        print("Toss live smoke disabled: pass --preflight to run read-only checks")
        return 0
    if not _truthy(os.environ.get("TOSS_API_ENABLED")):
        print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
        return 0
    return asyncio.run(run_preflight(args.symbol))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Verify Task 9**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py -q
uv run ruff check scripts/toss_live_smoke.py tests/services/brokers/toss/test_smoke_script.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 9**

```bash
git add scripts/toss_live_smoke.py tests/services/brokers/toss/test_smoke_script.py
git commit -m "feat(ROB-530): add Toss read-only smoke script"
```

## Task 10: No Mutation Surface Audit

**Files:**
- Create: `tests/services/brokers/toss/test_no_mutation_surface.py`

- [ ] **Step 1: Add static mutation audit**

Create `tests/services/brokers/toss/test_no_mutation_surface.py`:

```python
from __future__ import annotations

from pathlib import Path


TOSS_DIR = Path("app/services/brokers/toss")


def test_toss_client_has_no_order_mutation_methods() -> None:
    source = (TOSS_DIR / "client.py").read_text()

    forbidden = [
        "create_order",
        "modify_order",
        "cancel_order",
        "/api/v1/orders/{orderId}/modify",
        "/api/v1/orders/{orderId}/cancel",
        "\"POST\", \"/api/v1/orders\"",
        "'POST', '/api/v1/orders'",
    ]
    for needle in forbidden:
        assert needle not in source


def test_oauth_token_is_only_toss_post_in_client_package() -> None:
    sources = "\n".join(path.read_text() for path in TOSS_DIR.glob("*.py"))

    assert 'post("/oauth2/token"' in sources or "post(\n                \"/oauth2/token\"" in sources
    assert 'post("/api/v1/orders"' not in sources
    assert 'request("POST", "/api/v1/orders"' not in sources
```

- [ ] **Step 2: Run audit**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_no_mutation_surface.py -q
```

Expected: PASS.

- [ ] **Step 3: Fix formatting if needed**

Run:

```bash
uv run ruff format tests/services/brokers/toss/test_no_mutation_surface.py
uv run ruff check tests/services/brokers/toss/test_no_mutation_surface.py --fix
uv run pytest tests/services/brokers/toss/test_no_mutation_surface.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit Task 10**

```bash
git add tests/services/brokers/toss/test_no_mutation_surface.py
git commit -m "test(ROB-530): assert Toss client remains read-only"
```

## Task 11: Final Integration Verification

**Files:**
- All files changed by previous tasks.

- [ ] **Step 1: Run focused Toss suite**

Run:

```bash
uv run pytest tests/services/brokers/toss -q
```

Expected: PASS.

- [ ] **Step 2: Run config regression suite**

Run:

```bash
uv run pytest tests/test_config.py tests/services/brokers/toss/test_config.py -q
```

Expected: PASS.

- [ ] **Step 3: Run lint and type checks**

Run:

```bash
uv run ruff check app/core/config.py app/services/brokers/toss scripts/toss_live_smoke.py tests/services/brokers/toss
uv run ruff format --check app/core/config.py app/services/brokers/toss scripts/toss_live_smoke.py tests/services/brokers/toss
uv run ty check app/services/brokers/toss scripts/toss_live_smoke.py
```

Expected: PASS. If `ty` reports pre-existing unrelated project errors, capture the exact output and rerun with the narrowest supported path.

- [ ] **Step 4: Confirm no migration and no broker mutation**

Run:

```bash
git diff --name-only origin/main...HEAD | rg '^alembic/' || true
uv run pytest tests/services/brokers/toss/test_no_mutation_surface.py -q
```

Expected: no `alembic/` files printed; mutation audit PASS.

- [ ] **Step 5: Review diff for secret leaks**

Run:

```bash
rg -n "client_secret|get_secret_value\\(|TOSS_API_CLIENT_SECRET|Authorization|Bearer" app/services/brokers/toss scripts/toss_live_smoke.py tests/services/brokers/toss
```

Expected: `get_secret_value()` appears only inside the OAuth token request path and tests may mention sentinel secret strings. Runtime logs and exceptions mention env names only, not configured values.

- [ ] **Step 6: Add final Linear hold comment and label before PR/merge**

After implementation passes verification, update ROB-530 with a comment:

```text
Implementation is ready for ROB-530, but I am applying `hold_for_final_review` because this changes OAuth token coordination and live broker client foundations. No merge, deploy, or live operational use until stronger-model/CTO review clears token invalidation, host allowlist, retry, and read-only assumptions.
```

Apply the `hold_for_final_review` label in Linear.

- [ ] **Step 7: Final commit if verification changes were needed**

If any verification fixes were made after Task 10, commit them:

```bash
git add app/core/config.py app/services/brokers/toss scripts/toss_live_smoke.py tests/services/brokers/toss
git commit -m "test(ROB-530): verify Toss API client foundation"
```

## Self-Review Checklist

- [ ] ROB-530 config scope is covered by Task 1.
- [ ] Host allowlist and redirect refusal are covered by Task 2.
- [ ] Toss error envelope behavior, unknown codes, empty messages, and data hints are covered by Task 3.
- [ ] Redis single-flight token issuance, bounded contender wait, and dead-port hermetic Redis failure are covered by Task 4.
- [ ] Rate-limit groups and 09:00-09:10 KST peak rule are covered by Task 5.
- [ ] Decimal string parsing and float rejection are covered by Task 6.
- [ ] All read endpoints in ROB-530 scope are exposed by Task 7.
- [ ] Secret repr/log/error hygiene is covered by Tasks 1, 4, and 8.
- [ ] Smoke script is default-disabled and read-only in Task 9.
- [ ] Mutation surface is blocked by Task 10.
- [ ] Final verification includes no migration and stronger-review hold in Task 11.
