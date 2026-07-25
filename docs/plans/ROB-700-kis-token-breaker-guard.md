# ROB-700 — Guard the KIS OAuth token fetch with the circuit breaker

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Extend the ROB-699 per-process KIS circuit breaker to also guard the OAuth **token** fetch. ROB-699 wired the breaker into the data dispatch (`_request_with_rate_limit_with_headers`, `app/services/brokers/kis/base.py:431`), but LIVE measurement during the 2026-07-04 KIS maintenance proved it **never opens**: the only KIS HTTP calls made were `POST /oauth2/token` (half timing out at ~5s = the connect timeout), and ZERO data calls (`dailyprice`/`inquire-price`/`inquire-balance`). The reason is that `_fetch_token` (`base.py:266`) calls `cli.post("/oauth2/token")` (`base.py:277`) **directly on httpx**, bypassing the breaker-guarded dispatch. During maintenance the token fetch connect-times-out **first**, so no token is ever obtained, so no data call is ever attempted, so the breaker sees no failures and never opens (0 circuit-breaker log lines confirmed this). This change guards the token POST with the **same** singleton breaker: after N consecutive token connect-failures the breaker OPENS, and subsequent data calls fail-fast with `KISCircuitOpen` at the dispatch gate (~0ms), while open-breaker token fetches fail-fast at `_fetch_token`'s `before_request` — which **eliminates the 5s connect timeout** (the token fail-fast still runs *inside* the single-flight, so on an empty token cache it pays `refresh_token_with_lock`'s pre-lock cache double-checks — ~100ms from `2×asyncio.sleep(0.05)` + a few Redis round-trips — but **never** the 5s connect wait). Either way `/invest` KIS readers hit their existing Toss fallback / warning promptly instead of each burning the 5s connect timeout. KIS-healthy = pure passthrough; the cached-token fast path is byte-identical (no breaker involvement); migration-0.

## Architecture

### Current token-ensure / fetch flow (real refs)

Every KIS read/order call ensures a token before dispatch. The market-data read path calls `await self._parent._ensure_token()` before each request (`app/services/brokers/kis/_base_market_data.py:113`); order and account paths do the same (`domestic_orders.py:117`, `overseas_orders.py:97`, `account.py:221`, …).

- `_ensure_token()` (`base.py:293`):
  1. `token = await self._token_manager.get_token()` (`base.py:299`) — the **cache-hit fast path**. On a hit it sets `self._settings.kis_access_token = token` and `return`s (`base.py:300`–`:303`) **without ever calling `_fetch_token`**. No breaker involvement today, and none after this change.
  2. On a miss it defines `token_fetcher()` wrapping `self._fetch_token()` (`base.py:305`–`:307`) and hands it to `self._token_manager.refresh_token_with_lock(token_fetcher)` (`base.py:309`–`:311`).
- `refresh_token_with_lock(...)` (`app/services/redis_token_manager.py:243`) is the ROB-262 single-flight: it re-checks the cache up to 3× **before** the lock (`redis_token_manager.py:254`–`:260`), acquires the Redis distributed lock (`:264`), re-checks the cache **again after** acquiring the lock (`:289`–`:292`), and only THEN calls `token_fetcher()` (`:296`) — i.e. `_fetch_token` runs **exactly once per single-flight**, past all cache double-checks, under the lock. The surrounding `try/finally` (`:286`–`:307`) only **releases the lock** in `finally` and has **no `except`**, so an exception raised by `token_fetcher()` propagates out unswallowed.
- `_fetch_token()` (`base.py:266`–`:291`) — has **no try/except today**:
  ```
  cli = await self._ensure_client(timeout=5.0)          # base.py:276 (builds httpx client, no KIS network)
  r = await cli.post(self._kis_url("/oauth2/token"), data={...}, timeout=5)  # base.py:277–:285 — DIRECT httpx POST, bypasses the breaker
  response = r.json()                                    # base.py:286
  access_token = response["access_token"]               # base.py:287 — KeyError if KIS returns an error body
  expires_in = response.get("expires_in", 3600)         # base.py:288
  return access_token, expires_in                       # base.py:291
  ```

The breaker singleton and its API already exist (ROB-699, `app/services/brokers/kis/circuit_breaker.py`) and `get_kis_circuit_breaker` + `is_kis_connect_failure` are **already imported** in `base.py` (`base.py:21`–`:24`). No new import is needed.

**Consumer propagation (unchanged wiring):** a data read → `_request_with_token_retry` (`_base_market_data.py:113`) → `_ensure_token()` → (cache miss) `refresh_token_with_lock` → `_fetch_token` → **`KISCircuitOpen`** → propagates up through `refresh_token_with_lock`'s `try/finally` and `_ensure_token`, then is caught by the **existing** broad `except Exception` at each consumer: `InvestQuoteService._kis_fetch_kr` / `_kis_fetch_us` per-symbol fetch (`app/services/invest_quote_service.py:108`, `:125` → `None`) → `PriceFallbackResolver._apply_layer` fail-open (`app/services/invest_price_fallback.py:59` → Toss layer fills) and `KISHomeReader.fetch` (`app/services/invest_home_readers.py:301` → warning). `KISCircuitOpen` is a plain `Exception` subclass (`circuit_breaker.py:50`), so **no new fallback wiring** is required — same three broad-except sinks ROB-699 verified.

### Target breaker-guarded token flow

Guard the **network POST inside `_fetch_token`** (not `_ensure_token`, so the cache-hit fast path stays byte-identical), mirroring the ROB-699 dispatch wrapper (`base.py:431`–`:473`):

```
_fetch_token():
    breaker = get_kis_circuit_breaker()
    breaker.before_request()      # OPEN & pre-cooldown -> raise KISCircuitOpen NOW (no _ensure_client, no POST)
                                  # OUTSIDE the classify try/except (ROB-699 invariant)
    try:
        cli = await self._ensure_client(timeout=5.0)
        r   = await cli.post("/oauth2/token", data={...}, timeout=5)
        response = r.json()
        access_token = response["access_token"]
        expires_in   = response.get("expires_in", 3600)
    except BaseException as exc:
        if is_kis_connect_failure(exc):   # ConnectTimeout/ConnectError/PoolTimeout/ReadTimeout/ConnectionRefusedError
            breaker.record_failure()      # Nth consecutive connect-failure -> OPEN
        else:
            breaker.record_reachable_error()  # 401/invalid-key (KeyError) / non-JSON (ValueError) = KIS responded, must NOT trip
        raise                             # re-raise unchanged
    breaker.record_success()              # normal 2xx return resets the failure count / closes a probe
    logging.info("KIS 새 토큰 발급 완료")
    return access_token, expires_in
```

Behavior during maintenance: each `/invest` load whose token cache is empty drives **one** single-flight `_fetch_token` (single-flight preserved — the breaker check is per-attempt, inside `_fetch_token`, before the POST). Each token connect-failure records one `record_failure()`; after N consecutive the breaker opens; from then on both `_fetch_token` **and** the data dispatch `before_request()` fail-fast with `KISCircuitOpen`. The data-dispatch gate fail-fasts in ~0ms; the open-breaker token fetch fail-fasts at `_fetch_token` **inside** the single-flight (so on an empty token cache it still pays `refresh_token_with_lock`'s ~100ms pre-lock cache double-checks, but **never** the 5s connect wait). Either way KIS readers fall to Toss / warning promptly. The token+data guards share the **one** module singleton, so a token success (or a reachable non-2xx) resets the failure count for both, and vice-versa.

**Invariant (same as ROB-699):** `before_request()` MUST stay **outside** the classify `try/except`. If it were inside, a HALF_OPEN stampede `KISCircuitOpen` would be caught, classified "reachable" (`is_kis_connect_failure(KISCircuitOpen)` is `False`) and wrongly CLOSE the still-probing circuit. A dedicated stampede test locks this.

## Tech Stack

Python 3.13, uv, pytest + pytest-asyncio (`>=1.3`; explicit `@pytest.mark.asyncio`; markers `unit`/`asyncio`), httpx (transport exception hierarchy), the ROB-699 `KISCircuitBreaker` + module singleton (`app/services/brokers/kis/circuit_breaker.py`), stdlib `time.monotonic` / `logging`. No new dependency, no new config field, no Redis change, **migration-0** (no DB change).

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **Reuse the ROB-699 `circuit_breaker.py` module + singleton; do NOT create a second breaker.**
- **`before_request()` for the token POST sits OUTSIDE the classify `try/except`** (ROB-699 invariant). Add a guard test.
- **Trip ONLY on connect-level token failures** (`is_kis_connect_failure`: `ConnectTimeout`/`ConnectError`/`PoolTimeout`/`ReadTimeout`/`ConnectionRefusedError`); **401 / invalid-key / business token errors are reachable and must NOT trip** (test this).
- **Cached-token fast path is byte-identical** (no breaker involvement on cache hit) — prove by test.
- **KIS-healthy = passthrough** (breaker closed); flag `kis_circuit_breaker_enabled=False` = pure no-op. **migration-0.** No change to the token single-flight / Redis-cache semantics.
- **Deterministic tests:** reuse the ROB-699 injected clock + singleton-reset conftest fixture (`tests/conftest.py:410` `_isolate_kis_circuit_breaker`); fake httpx; no real sleep / network.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do **NOT** commit unless the executing skill says so; each task lists its own commit message.

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/services/brokers/kis/base.py` | Modify | Task 1 — guard the `_fetch_token` (`:266`) network POST with `get_kis_circuit_breaker().before_request()` (outside the classify try) + `record_success`/`record_failure`/`record_reachable_error`. Reuses the existing import (`:21`–`:24`). |
| `tests/test_kis_token_circuit_breaker.py` | Create | Task 1 tests — `_fetch_token` breaker behavior with faked httpx (connect trips, zero-HTTP-when-open, 401/non-JSON do not trip, success resets, disabled passthrough, half-open stampede invariant, cache-hit byte-identical). |
| `tests/test_kis_token_circuit_open_propagation.py` | Create | Task 2 tests — `KISCircuitOpen` from `_fetch_token` survives the single-flight (`refresh_token_with_lock` re-raises + releases lock) and propagates through `_ensure_token` with ZERO token HTTP. |
| `docs/runbooks/kis-circuit-breaker.md` | Modify | Task 3 — replace the now-wrong "OAuth token endpoint is not breaker-guarded" scope note; document the token guard, the maintenance sequence, and that single-flight/cache semantics are unchanged. |

> **NOT touched:** the ROB-699 data dispatch guard (`_request_with_rate_limit_with_headers`, `base.py:431`) and the extracted `_dispatch_rate_limited_with_headers` (`base.py:475`) stay byte-for-byte. `_ensure_token`'s cache-hit fast path (`base.py:299`–`:303`) is unchanged — the guard is added strictly inside `_fetch_token`'s network branch, which the cache-hit path never reaches. `refresh_token_with_lock` / `RedisTokenManager` single-flight + Redis-cache semantics (`redis_token_manager.py`) are unchanged (the breaker check is per-attempt, inside `_fetch_token`, before the POST). No new config field — reuse `kis_circuit_breaker_*` (`app/core/config.py:336`–`:342`). The retry/rate-limit dispatch, `_execute_http_request`, `_parse_kis_response`, and every order/holdings/quote mutation path are unchanged. No caller gets new try/except — `PriceFallbackResolver`, `InvestQuoteService`, and `KISHomeReader` already catch broad `Exception`. migration-0.

---

## Task 1 — Guard the `_fetch_token` network POST with the circuit breaker (migration-0)

**Files:**
- Modify `app/services/brokers/kis/base.py` — wrap the body of `_fetch_token` (`:266`–`:291`). The breaker import (`get_kis_circuit_breaker`, `is_kis_connect_failure`) is **already present** at `base.py:21`–`:24`; no import change.
- Test (create) `tests/test_kis_token_circuit_breaker.py`.

**Interfaces:**
- `_fetch_token(self) -> tuple[str, int]` keeps its **exact** signature and return type (`base.py:266`). It now calls `get_kis_circuit_breaker().before_request()` first (may raise `KISCircuitOpen` — a plain `Exception`), then runs `_ensure_client` + the POST + parse inside a `try/except BaseException` that calls `record_failure()` (connect) / `record_reachable_error()` (else) before re-raising, and `record_success()` on the normal return.
- Reused breaker API (no change): `get_kis_circuit_breaker()` (`circuit_breaker.py:180`), `is_kis_connect_failure(exc)` (`circuit_breaker.py:65`), `KISCircuitBreaker.before_request` / `record_success` / `record_failure` / `record_reachable_error` (`circuit_breaker.py:115`–`:174`), `KISCircuitOpen` (`circuit_breaker.py:50`).

Steps:

- [ ] **Write the failing tests — token breaker behavior at the fetch seam.** Create `tests/test_kis_token_circuit_breaker.py`:
```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.circuit_breaker import KISCircuitBreaker, KISCircuitOpen

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeSettings:
    def __init__(self) -> None:
        self.kis_app_key = "key"
        self.kis_app_secret = "secret"
        self.kis_access_token = "token"
        self.kis_base_url = "https://openapi.koreainvestment.com:9443"


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        type(self)._shared_client_lock = None
        self._fake_settings = _FakeSettings()

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return self._fake_settings


class _BreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 3
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture(autouse=True)
def _install_breaker(clock):
    # Inject a deterministic-clock, ENABLED breaker as THE process singleton.
    # (conftest._isolate_kis_circuit_breaker disables the GLOBAL settings flag,
    # but this breaker reads its own settings_obj, so it stays enabled here.)
    cb._breaker = KISCircuitBreaker(now=clock.now, settings_obj=_BreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


def _client_with_post(post):
    client = _FakeClient()
    fake_http = MagicMock()
    fake_http.post = post
    client._ensure_client = AsyncMock(return_value=fake_http)  # type: ignore[method-assign]
    return client


def _json_response(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout(""),
        httpx.ConnectError(""),
        httpx.PoolTimeout(""),
        httpx.ReadTimeout(""),
        ConnectionRefusedError(),
    ],
)
async def test_token_connect_failures_open_breaker(exc):
    post = AsyncMock(side_effect=exc)
    client = _client_with_post(post)
    for _ in range(3):  # threshold
        with pytest.raises(type(exc)):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "open"


@pytest.mark.asyncio
async def test_open_breaker_token_fetch_zero_http():
    post = AsyncMock(side_effect=httpx.ConnectError(""))
    client = _client_with_post(post)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "open"
    post.reset_mock()
    client._ensure_client.reset_mock()
    with pytest.raises(KISCircuitOpen):
        await client._fetch_token()
    post.assert_not_awaited()  # ZERO HTTP
    client._ensure_client.assert_not_awaited()  # not even the client build


@pytest.mark.asyncio
async def test_token_401_invalid_key_does_not_open():
    # KIS responded with an error body lacking access_token -> KeyError (reachable).
    post = AsyncMock(return_value=_json_response({"error": "invalid_client"}))
    client = _client_with_post(post)
    for _ in range(6):  # well past threshold
        with pytest.raises(KeyError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert cb.get_kis_circuit_breaker().failure_count == 0


@pytest.mark.asyncio
async def test_token_non_json_body_does_not_open():
    r = MagicMock()
    r.json.side_effect = ValueError("not json")
    post = AsyncMock(return_value=r)
    client = _client_with_post(post)
    for _ in range(6):
        with pytest.raises(ValueError):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_token_success_returns_and_keeps_closed():
    post = AsyncMock(return_value=_json_response({"access_token": "T", "expires_in": 100}))
    client = _client_with_post(post)
    token, expires = await client._fetch_token()
    assert token == "T"
    assert expires == 100
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_token_success_after_failures_resets_count():
    post = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout(""),
            httpx.ConnectTimeout(""),
            _json_response({"access_token": "T", "expires_in": 100}),
        ]
    )
    client = _client_with_post(post)
    for _ in range(2):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    assert cb.get_kis_circuit_breaker().failure_count == 2
    await client._fetch_token()  # success resets the counter
    assert cb.get_kis_circuit_breaker().failure_count == 0
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_disabled_flag_token_passthrough():
    class _Disabled(_BreakerSettings):
        kis_circuit_breaker_enabled = False

    cb._breaker = KISCircuitBreaker(settings_obj=_Disabled())
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client = _client_with_post(post)
    for _ in range(10):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    # never opens; every call still reached the network (passthrough)
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert post.await_count == 10


@pytest.mark.asyncio
async def test_half_open_probe_stampede_does_not_close(clock):
    # Locks "before_request() OUTSIDE the classify try/except": once a half-open
    # probe is in flight, a stampede caller must raise KISCircuitOpen WITHOUT
    # that raise being reclassified reachable (which would wrongly close the
    # still-probing circuit).
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client = _client_with_post(post)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await client._fetch_token()
    breaker = cb.get_kis_circuit_breaker()
    assert breaker.state == "open"
    clock.advance(45)
    breaker.before_request()  # hands out THE probe -> half_open, in flight
    assert breaker.state == "half_open"
    with pytest.raises(KISCircuitOpen):
        await client._fetch_token()  # stampede caller: must fail-fast
    assert breaker.state == "half_open"  # still probing, NOT closed


@pytest.mark.asyncio
async def test_cached_token_path_no_breaker_involvement():
    # _ensure_token cache-hit must be byte-identical: _fetch_token never called,
    # breaker never touched (a pre-loaded failure count is preserved).
    breaker = cb.get_kis_circuit_breaker()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.failure_count == 2

    client = _FakeClient()
    client._token_manager = MagicMock()
    client._token_manager.get_token = AsyncMock(return_value="cached-tok")
    client._fetch_token = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("cache hit must not fetch")
    )

    await client._ensure_token()
    assert client._settings.kis_access_token == "cached-tok"
    client._fetch_token.assert_not_awaited()
    assert breaker.failure_count == 2  # untouched
    assert breaker.state == "closed"
```

- [ ] **Run it — fails.** `uv run pytest tests/test_kis_token_circuit_breaker.py -v`
  Expected: the connect-trip, zero-HTTP, success-resets and stampede tests FAIL (`_fetch_token` has no breaker calls yet — the breaker never opens, `KISCircuitOpen` is never raised). The 401 / non-JSON / disabled / cache-hit tests pass coincidentally (closed passthrough / cache-hit already skips fetch). Keep them to lock the boundaries.

- [ ] **Minimal impl — guard `_fetch_token`.** In `app/services/brokers/kis/base.py`, replace the body of `_fetch_token` (`:266`–`:291`) with the guarded version (the `def`/signature and the POST payload are unchanged; only the breaker calls + `try/except` are added). `before_request()` is placed **before** the `try` (so an open circuit does zero work); `_ensure_client` + POST + parse go **inside** the `try`:
```python
    async def _fetch_token(self) -> tuple[str, int]:
        """Fetch new OAuth2 token from KIS API.

        ROB-700: the token POST is guarded by the SAME per-process circuit
        breaker as the data dispatch (ROB-699). During a KIS maintenance window
        the token fetch connect-times-out FIRST (before any data call), so
        guarding it here is what lets the breaker open at all. After N
        consecutive token connect-failures ``before_request`` fail-fasts with
        ``KISCircuitOpen`` (zero HTTP, zero wait) and every KIS reader hits its
        Toss fallback / warning immediately. A 401 / invalid-key / non-JSON
        body means KIS RESPONDED (reachable) and must NOT trip the breaker.

        The breaker check is per-attempt, inside this call, before the network
        POST — the token single-flight (``refresh_token_with_lock``) and the
        cached-token fast path in ``_ensure_token`` are unchanged.

        Returns:
            Tuple of (access_token, expires_in_seconds)

        Raises:
            KISCircuitOpen: When the breaker is open (fail-fast, no HTTP).
            httpx.HTTPStatusError / httpx.RequestError: On HTTP/transport errors.
            KeyError: If response doesn't contain access_token.
        """
        breaker = get_kis_circuit_breaker()
        breaker.before_request()  # OUTSIDE the classify try/except (ROB-699 invariant)
        try:
            cli = await self._ensure_client(timeout=5.0)
            r = await cli.post(
                self._kis_url("/oauth2/token"),
                data={
                    "grant_type": "client_credentials",
                    "appkey": self._settings.kis_app_key,
                    "appsecret": self._settings.kis_app_secret,
                },
                timeout=5,
            )
            response = r.json()
            access_token = response["access_token"]
            expires_in = response.get("expires_in", 3600)
        except BaseException as exc:  # noqa: BLE001 — classify then re-raise unchanged
            if is_kis_connect_failure(exc):
                breaker.record_failure()  # connect/read outage -> trips after N
            else:
                breaker.record_reachable_error()  # KIS responded (401/KeyError/JSON) — no trip
            raise
        breaker.record_success()
        logging.info("KIS 새 토큰 발급 완료")
        return access_token, expires_in
```
**Critical invariant:** `breaker.before_request()` MUST stay **outside** the `try/except BaseException`. If a refactor moves it inside, a HALF_OPEN stampede `KISCircuitOpen` gets caught, classified "reachable" (`is_kis_connect_failure(KISCircuitOpen)` is `False`), and `record_reachable_error()` would wrongly CLOSE the still-probing circuit. `test_half_open_probe_stampede_does_not_close` locks this.

- [ ] **Run it — passes.** `uv run pytest tests/test_kis_token_circuit_breaker.py -v` → all pass.

- [ ] **Regression — the existing token/dispatch surface stays green.** `uv run pytest tests/test_services_kis_client.py tests/test_kis_circuit_breaker_dispatch.py tests/services/brokers/kis/test_circuit_breaker.py tests/test_redis_token_manager.py tests/test_kis_base_rate_limit.py -v`
  Expected: all pass unchanged — the conftest `_isolate_kis_circuit_breaker` fixture (`tests/conftest.py:410`) forces the breaker OFF for every existing test, so the token guard is a pure no-op there and `_fetch_token` behaves byte-identically.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-700): guard KIS OAuth token fetch with the circuit breaker (fail-fast on token connect outage)"`

---

## Task 2 — Single-flight propagation regression: `KISCircuitOpen` survives the token lock (test-only, migration-0)

**Files:**
- Test (create) `tests/test_kis_token_circuit_open_propagation.py` — proves (a) `refresh_token_with_lock` re-raises a fetcher `KISCircuitOpen` and still releases the lock, and (b) an open breaker makes `_ensure_token` fail-fast with `KISCircuitOpen` and **zero** token HTTP, exercising the REAL single-flight.
- **No source change** — `refresh_token_with_lock` (`redis_token_manager.py:286`–`:307`) has no `except` that swallows, `KISCircuitOpen` is a plain `Exception` (`circuit_breaker.py:50`), and the three consumer fallbacks already catch broad `Exception` (ROB-699-verified). The resolver-layer catch is additionally locked by the existing `tests/test_invest_price_fallback_circuit_open.py` (source-agnostic — any `KISCircuitOpen`, including a token-origin one, fails through to Toss).

**Interfaces:** none produced. Consumes `RedisTokenManager.refresh_token_with_lock`, `BaseKISClient._ensure_token`, `KISCircuitBreaker`, `KISCircuitOpen`.

Steps:

- [ ] **Write the regression tests.** Create `tests/test_kis_token_circuit_open_propagation.py`:
```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.circuit_breaker import KISCircuitBreaker, KISCircuitOpen
from app.services.redis_token_manager import RedisTokenManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeSettings:
    def __init__(self) -> None:
        self.kis_app_key = "key"
        self.kis_app_secret = "secret"
        self.kis_access_token = "token"
        self.kis_base_url = "https://openapi.koreainvestment.com:9443"


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        type(self)._shared_client_lock = None
        self._fake_settings = _FakeSettings()

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return self._fake_settings


class _EnabledBreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 1  # 1 failure -> open
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture(autouse=True)
def _install_breaker():
    cb._breaker = KISCircuitBreaker(settings_obj=_EnabledBreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


async def test_refresh_token_with_lock_reraises_circuit_open_and_releases_lock(
    monkeypatch,
):
    # The token single-flight must NOT swallow KISCircuitOpen and must still
    # release the distributed lock. Stub the cache/lock probes so the real
    # try/finally body runs with no real sleep / no real Redis.
    monkeypatch.setattr(
        "app.services.redis_token_manager.asyncio.sleep", AsyncMock()
    )
    manager = RedisTokenManager()
    manager.get_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    manager._acquire_lock = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._release_lock = AsyncMock()  # type: ignore[method-assign]
    manager.save_token = AsyncMock()  # type: ignore[method-assign]

    fetcher = AsyncMock(side_effect=KISCircuitOpen(45.0))

    with pytest.raises(KISCircuitOpen):
        await manager.refresh_token_with_lock(fetcher)

    fetcher.assert_awaited_once()  # single-flight: exactly one fetch attempt
    manager._release_lock.assert_awaited()  # lock released on the error path
    manager.save_token.assert_not_awaited()  # no token persisted on failure


async def test_open_breaker_ensure_token_fails_fast_zero_http(monkeypatch):
    # End-to-end: open breaker -> _ensure_token (cache miss) -> single-flight ->
    # _fetch_token.before_request() -> KISCircuitOpen, with ZERO token POST.
    monkeypatch.setattr(
        "app.services.redis_token_manager.asyncio.sleep", AsyncMock()
    )
    breaker = cb.get_kis_circuit_breaker()
    breaker.record_failure()  # threshold=1 -> OPEN
    assert breaker.state == "open"

    manager = RedisTokenManager()
    manager.get_token = AsyncMock(return_value=None)  # cache miss everywhere
    manager._acquire_lock = AsyncMock(return_value=True)
    manager._release_lock = AsyncMock()
    manager.save_token = AsyncMock()

    client = _FakeClient()
    client._token_manager = manager
    post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    fake_http = MagicMock()
    fake_http.post = post
    client._ensure_client = AsyncMock(return_value=fake_http)  # type: ignore[method-assign]

    with pytest.raises(KISCircuitOpen):
        await client._ensure_token()

    post.assert_not_awaited()  # ZERO token HTTP — failed fast at before_request
    client._ensure_client.assert_not_awaited()
    manager._release_lock.assert_awaited()  # single-flight lock still released
```

- [ ] **Run it — passes on first run (contract lock).** `uv run pytest tests/test_kis_token_circuit_open_propagation.py -v`
  Both tests are GREEN immediately (Task 2 runs **after** Task 1's `_fetch_token` guard is already in place): the first (`test_refresh_token_with_lock_reraises_circuit_open_and_releases_lock`) passes because `refresh_token_with_lock` already re-raises via its `try/finally` and `KISCircuitOpen` is a plain `Exception`; the second (`test_open_breaker_ensure_token_fails_fast_zero_http`) additionally exercises the Task 1 `before_request` guard end-to-end through the real `_ensure_token` (it would FAIL if run before Task 1's impl — the token POST would be awaited and raise `ConnectTimeout` instead of `KISCircuitOpen`). Their job is to LOCK the "single-flight does not swallow the fail-fast + lock is still released" guarantee and the "open breaker => zero token HTTP through the real `_ensure_token`" behavior. (If either is unexpectedly red, stop and reconcile — do not add wiring blindly.)

- [ ] **Cross-check the resolver hop already covered.** `uv run pytest tests/test_invest_price_fallback_circuit_open.py -v`
  Expected: all pass unchanged — the ROB-699 resolver test already proves a `KISCircuitOpen` (regardless of origin) fails through the KIS layer to Toss/snapshot, so the token-origin case needs no new resolver test.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "test(ROB-700): lock KISCircuitOpen propagation through the token single-flight"`

---

## Task 3 — Update the operator runbook (docs, migration-0)

**Files:**
- Modify `docs/runbooks/kis-circuit-breaker.md` — the ROB-699 runbook currently states (`:99`–`:101`) "**The OAuth token endpoint is not breaker-guarded.**" That is now **wrong**; update it and add the ROB-700 rationale.

**Interfaces:** none.

Steps:

- [ ] **Update "What it does".** Note that the breaker now guards **both** the data dispatch seam **and** the OAuth token POST (`_fetch_token`, `app/services/brokers/kis/base.py`), sharing the one module singleton.

- [ ] **Add a "Why the token fetch is also guarded (ROB-700)" subsection.** Document the LIVE finding: during the 2026-07-04 KIS maintenance the ROB-699 data guard never opened (0 circuit logs) because the ONLY KIS HTTP calls were `POST /oauth2/token` (half timing out at ~5s) and ZERO data calls — the token fetch connect-times-out **first**, so no token → no data call → no data-dispatch failure → breaker never trips. Guarding `_fetch_token` fixes this: N consecutive **token** connect-failures now open the breaker, after which both token fetches and data calls fail-fast.

- [ ] **Replace the scope note.** Remove "The OAuth token endpoint is not breaker-guarded" (`:99`–`:101`) and replace with: the OAuth token POST **is** guarded (ROB-700); the **cached-token fast path is byte-identical** (a cache hit in `_ensure_token` never calls `_fetch_token`, so the breaker is never touched); the token **single-flight / Redis-cache semantics are unchanged** (the breaker check is per-attempt, inside `_fetch_token`, before the POST); and **401 / invalid-key / non-JSON** token errors are *reachable* and do **not** trip (only transport connect/read-hang failures do). Also note that because token and data share one singleton, a token success (or reachable non-2xx) resets the failure count for both surfaces.

- [ ] **Confirm the classifier / defaults / log-line sections still read correctly** — they already apply verbatim to the token POST (same `is_kis_connect_failure` set, same `kis_circuit_breaker_*` defaults at `app/core/config.py:336`–`:342`, same WARNING/INFO log lines). No change needed beyond the token-scope additions above.

- [ ] **Full regression sweep.** `uv run pytest tests/test_kis_token_circuit_breaker.py tests/test_kis_token_circuit_open_propagation.py tests/test_kis_circuit_breaker_dispatch.py tests/services/brokers/kis/test_circuit_breaker.py tests/test_invest_price_fallback_circuit_open.py tests/test_services_kis_client.py tests/test_redis_token_manager.py -v`
  Expected: all pass — new token suites green, ROB-699 breaker + dispatch + fallback + existing KIS client/token-manager suites unaffected.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "docs(ROB-700): runbook — OAuth token fetch is now breaker-guarded"`

---

## Rollout / verification notes (non-blocking)

- **No new config / defaults reused.** ROB-700 reuses `kis_circuit_breaker_enabled` / `_failure_threshold` (5) / `_cooldown_seconds` (45) unchanged (`app/core/config.py:336`–`:342`). `KIS_CIRCUIT_BREAKER_ENABLED=false` disables both the data and token guards with zero deploy.
- **First-load caveat still holds.** Opening the breaker requires N consecutive token connect-failures; because the token single-flight collapses a concurrent `/invest` burst into ONE `_fetch_token`, it takes ~N separate refresh cycles (loads) to open. This is the intended steady-state behavior — the breaker is a steady-state fail-fast guard, not a first-request guard.
- **Token+data share one breaker (intended).** During real KIS maintenance both the token host and the data host are down together, so a token failure count and a data failure count reinforce each other, and any proof of reachability (token success OR a data 2xx) resets both. Revisit only if false-coupling is observed in practice.
- **Live re-measure after rollout.** The ROB-699 outage produced 0 circuit logs; after ROB-700 the next KIS-maintenance window should show `WARNING "KIS circuit OPEN after N consecutive connect-failures"` once token connect-failures cross the threshold, then `/invest` KIS readers falling to Toss in ~0ms. Confirm from production before closing ROB-700.
