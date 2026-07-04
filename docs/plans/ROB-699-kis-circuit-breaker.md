# ROB-699 — KIS Client Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Add a per-process, in-process circuit breaker to the shared KIS HTTP client so that when the KIS host is unreachable (e.g. the 2026-07-04 KIS maintenance), KIS transport connect-failures **fail-fast** with a `KISCircuitOpen` exception instead of every KIS-dependent `/invest` reader waiting the full connect-timeout × retries on every call. Today `/invest/api/home` averages 27.5s during a KIS outage (Sentry): the ROB-696 price service is KIS-first → Toss fallback, so a single `/invest` load fires ~24 KIS `inquire_price`/`inquire_overseas_daily_price` calls that each burn the ~5s connect-timeout (× retries) before their per-symbol `except` swallows to `None` and the Toss layer (which serves 100%) fills. Once the breaker is open, those calls raise `KISCircuitOpen` in ~0ms, the **existing** fallbacks fire immediately, and no new fallback wiring is added. When KIS is healthy the breaker is closed and is a pure passthrough — every existing KIS test stays green.

## Architecture

### Current KIS dispatch / retry flow (real refs)

All KIS sub-clients inherit `BaseKISClient` (`app/services/brokers/kis/base.py:71`). Every data request funnels through a single dispatch method:

- `_request_with_rate_limit(...)` (`base.py:375`) — the public read/write entrypoint — delegates verbatim to `_request_with_rate_limit_with_headers(...)` (`base.py:413`).
- `_request_with_rate_limit_with_headers(...)` (`base.py:427`) is **the single HTTP dispatch**. Its body (`base.py:453`–`:570`):
  1. `parsed_url = urlparse(url)` and builds `api_key` (`:453`–`:455`).
  2. Resolves the per-API limiter: `_get_rate_limit_for_api` (`:457`) + `_get_limiter` (`:458`).
  3. Computes `max_retries` from `max_retries_override` or `settings.api_rate_limit_retry_429_max` (`:459`–`:463`) — **ROB-645**: order-submission call sites pass `max_retries_override=0` + `retry_request_errors=False` so a timed-out order POST is sent exactly once.
  4. Retry loop `for attempt in range(max_retries + 1)` (`:467`):
     - `await limiter.acquire(...)` — **the rate-limit wait** (`:468`).
     - `client = await self._ensure_client(...)` (`:478`), `response = await self._execute_http_request(...)` (`:479`) — the innermost single GET/POST (`_execute_http_request`, `base.py:317`).
     - Success path: `return data, dict(response.headers)` (`:529`).
     - `except httpx.HTTPStatusError` (`:531`): retry on 429 else `raise` (`:547`).
     - `except httpx.RequestError` (`:548`): retry when `retry_request_errors and attempt < max_retries` else `raise` (`:565`). Transient httpx transport/timeout errors (`ConnectTimeout`, `ConnectError`, `PoolTimeout`, `ReadTimeout`, …) are all `httpx.RequestError` subclasses and land here.
     - Loop exhausted (all attempts were 429/heuristic `continue`) → `raise RateLimitExceededError(...)` (`:567`).

Exception messaging already uses `describe_exception` (`app/core/exceptions.py:6`, imported at `base.py:20`) which surfaces the class name for empty-message httpx timeouts (ROB-600).

**Consumer path that motivates this (ROB-696):** `InvestQuoteService._kis_fetch_kr` / `_kis_fetch_us` (`app/services/invest_quote_service.py:101`, `:115`) fan out per-symbol `MarketDataClient.inquire_price` / `inquire_overseas_daily_price` under `asyncio.gather`; each `_fetch` has its own `except Exception` (`:108`, `:125`) that swallows to `results[symbol] = None`. The layer result feeds `PriceFallbackResolver.resolve` (`app/services/invest_price_fallback.py:35`) whose `_apply_layer` wraps each layer `except Exception` fail-open (`:59`) → KIS empty → Toss layer fills. The account reader `KISHomeReader.fetch` (`app/services/invest_home_readers.py:83`) wraps the whole KIS account flow in `except Exception` (`:301`) → warning. **All three catch a broad `Exception`, so a plain-`Exception` `KISCircuitOpen` propagates through them with zero new wiring.**

### Target breaker-guarded flow

A module-level singleton `KISCircuitBreaker` (new module `app/services/brokers/kis/circuit_breaker.py`) is consulted at the **top** of `_request_with_rate_limit_with_headers`, strictly **above** the limiter acquire (`base.py:468`) and the retry loop (`base.py:467`):

```
_request_with_rate_limit_with_headers(...):
    breaker = get_kis_circuit_breaker()
    breaker.before_request()          # OPEN & pre-cooldown -> raise KISCircuitOpen NOW
                                      # (no _get_limiter, no limiter.acquire, no HTTP)
    try:
        data, headers = await self._dispatch_rate_limited_with_headers(...)   # <- today's body verbatim
    except BaseException as exc:
        if is_kis_connect_failure(exc):   # ConnectTimeout/ConnectError/PoolTimeout/ReadTimeout/ConnectionRefusedError
            breaker.record_failure()      # Nth consecutive connect-failure -> OPEN
        else:
            breaker.record_reachable_error()  # 429 / HTTPStatusError / RuntimeError / RateLimitExceeded = KIS is up
        raise
    breaker.record_success()          # any normal 2xx return resets the failure count / closes a probe
    return data, headers
```

State machine (per process, shared across every `KISClient`/`BaseKISClient` instance, live **and** mock):

```
CLOSED  --(N consecutive connect-failures)-->  OPEN
OPEN    --(before_request while now-opened_at < cooldown)-->  raise KISCircuitOpen  (0 HTTP, 0 wait)
OPEN    --(before_request when now-opened_at >= cooldown)-->  HALF_OPEN, hand out EXACTLY ONE probe
HALF_OPEN(probe in flight) --(before_request)-->  raise KISCircuitOpen  (stampede guard)
HALF_OPEN  --(probe success / probe reached KIS)-->  CLOSED (failures=0)
HALF_OPEN  --(probe connect-failure)-->  OPEN (opened_at=now)
```

Clock is **injected** (`now` callable, default `time.monotonic`) for deterministic tests. `before_request()` is **synchronous** — because asyncio is single-threaded and there is no `await` between the half-open check and the `_probe_in_flight = True` set, a concurrent burst of half-open calls yields exactly one probe with no `asyncio.Lock`. Thresholds are read lazily from `settings` so the enable flag and test overrides are always live; `kis_circuit_breaker_enabled=False` makes `before_request()`/`record_*()` pure no-ops (complete passthrough).

## Tech Stack

Python 3.13, uv, pytest + pytest-asyncio (`>=1.3` — tests use explicit `@pytest.mark.asyncio`; markers `unit`/`asyncio`), httpx (transport exception hierarchy), pydantic-settings `Settings` (`app/core/config.py:184`), stdlib `time.monotonic` / `logging`. No new dependency, no Redis, **migration-0** (no DB change).

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **KIS-healthy behavior is UNCHANGED** (breaker closed = pure passthrough; every existing KIS test stays green).
- **Flag `kis_circuit_breaker_enabled=False` = complete no-op** (passthrough), proven by test.
- **Trips ONLY on transport connect/read-hang failures** (`ConnectTimeout`/`ConnectError`/`PoolTimeout`/`ReadTimeout`/`ConnectionRefusedError`); 429 / business / app errors and `WriteTimeout` must NOT open the breaker (test this explicitly).
- **Per-process MODULE-LEVEL singleton** (shared across `KISClient` instances), NOT per-instance, NOT Redis.
- **Open state raises `KISCircuitOpen` with ZERO HTTP and ZERO rate-limit wait**, and it propagates to existing fallbacks (no new fallback wiring).
- **Half-open allows EXACTLY ONE probe**; concurrent half-open calls do not stampede.
- **No broker mutation logic changes** — the breaker only fail-fasts on transport failure; order/holdings/quote semantics unchanged. **migration-0**.
- **Deterministic tests:** inject the clock; no real sleeps; reset the module singleton between tests (a fixture).
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/core/config.py` | Modify | Task 1 — add `kis_circuit_breaker_enabled` / `_failure_threshold` / `_cooldown_seconds` to `Settings` (after the 429-retry block, `:330`). |
| `app/services/brokers/kis/circuit_breaker.py` | Create | Task 2 — `KISCircuitOpen`, `is_kis_connect_failure`, `KISCircuitBreaker` (injected clock, sync `before_request`, half-open single-probe), module singleton `get_kis_circuit_breaker` / `reset_kis_circuit_breaker`. |
| `app/services/brokers/kis/base.py` | Modify | Task 3 — extract the dispatch body (`:453`–`:570`) into `_dispatch_rate_limited_with_headers`; make `_request_with_rate_limit_with_headers` (`:427`) the thin breaker wrapper (`before_request` guard + `record_*`). |
| `tests/services/brokers/kis/test_circuit_breaker.py` | Create | Task 2 tests — pure state-machine (fake clock + fake settings + singleton-reset fixture). |
| `tests/test_kis_circuit_breaker_dispatch.py` | Create | Task 3 tests — breaker wired into `BaseKISClient` dispatch with faked httpx (zero-HTTP-when-open, connect trips, 429 does not trip, disabled passthrough, healthy unchanged). |
| `tests/test_invest_price_fallback_circuit_open.py` | Create | Task 4 tests — `KISCircuitOpen` propagates through the existing `_apply_layer` / per-symbol fetch fail-open (no new wiring). |
| `tests/conftest.py` | Modify | Task 3 — autouse **global** fixture that resets the module singleton AND forces `kis_circuit_breaker_enabled=False` per test, so the enabled-by-default per-process breaker is a guaranteed no-op across the entire existing KIS suite (kills cross-test singleton leak/flake). |
| `docs/runbooks/kis-circuit-breaker.md` | Create | Task 4 — operator note: what trips it, defaults, how to disable, observability log lines. |

> **NOT touched:** the retry loop's classifier semantics (`retry_request_errors` / `max_retries_override` / 429 heuristic / `RateLimitExceededError` — ROB-270/ROB-645) are moved verbatim into `_dispatch_rate_limited_with_headers`, **byte-for-byte unchanged**; `_execute_http_request` (`base.py:317`), `_parse_kis_response` (`base.py:335`), token management (`_fetch_token`/`_ensure_token`, `base.py:262`/`:289`), and every order/holdings/quote mutation path stay as-is. No caller of KIS gets new try/except — `PriceFallbackResolver`, `InvestQuoteService`, and `KISHomeReader` already catch broad `Exception`. The KIS OAuth token endpoint (`/oauth2/token`) is intentionally **not** breaker-guarded (different host path; Redis-cached token means the hot price path does not hit it). migration-0.

---

## Task 1 — Config flags for the KIS circuit breaker (migration-0)

**Files:**
- Modify `app/core/config.py` — add three fields to `Settings` (`:184`) directly after `api_rate_limit_retry_429_base_delay` (`:330`), before the `# Telegram` block (`:331`).
- Test (create) `tests/services/brokers/kis/test_circuit_breaker.py` (config assertions live in the same new module used by Task 2; add the config test class now).

**Interfaces:**
- Produces `Settings.kis_circuit_breaker_enabled: bool = True`, `Settings.kis_circuit_breaker_failure_threshold: int = 5`, `Settings.kis_circuit_breaker_cooldown_seconds: int = 45` (mirrors the existing `bool` + `int` field style, e.g. `kis_rate_limit_rate: int = 19` at `:282`).

Steps:

- [ ] **Write the failing test — defaults present and typed.** Create `tests/services/brokers/kis/test_circuit_breaker.py` with this first block:
```python
from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


class TestCircuitBreakerSettings:
    def test_defaults(self):
        s = Settings()
        assert s.kis_circuit_breaker_enabled is True
        assert s.kis_circuit_breaker_failure_threshold == 5
        assert s.kis_circuit_breaker_cooldown_seconds == 45

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_ENABLED", "false")
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")
        monkeypatch.setenv("KIS_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "10")
        s = Settings()
        assert s.kis_circuit_breaker_enabled is False
        assert s.kis_circuit_breaker_failure_threshold == 3
        assert s.kis_circuit_breaker_cooldown_seconds == 10
```

- [ ] **Run it — fails.** `uv run pytest tests/services/brokers/kis/test_circuit_breaker.py -k CircuitBreakerSettings -v`
  Expected: `AttributeError` — the three fields do not exist on `Settings` yet. (Confirm no name clash: `grep -n "circuit" app/core/config.py` returns nothing today.)

- [ ] **Minimal impl — add the fields.** In `app/core/config.py`, immediately after line 330 (`api_rate_limit_retry_429_base_delay: float = 0.2  # 지수 백오프 기본 대기 시간 (초)`), insert:
```python

    # ROB-699: per-process in-process circuit breaker for KIS transport connect
    # failures (e.g. KIS maintenance). Closed = pure passthrough; open = fail-fast
    # so /invest KIS→Toss fallbacks fire in ~0ms instead of burning the connect
    # timeout on every call. Default ON; False = complete no-op.
    kis_circuit_breaker_enabled: bool = True
    kis_circuit_breaker_failure_threshold: int = 5  # consecutive connect-failures -> open
    kis_circuit_breaker_cooldown_seconds: int = 45  # open -> half-open cooldown (monotonic s)
```

- [ ] **Run it — passes.** `uv run pytest tests/services/brokers/kis/test_circuit_breaker.py -k CircuitBreakerSettings -v` → 2 passed.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-699): add kis_circuit_breaker_* settings (enabled/threshold/cooldown)"`

---

## Task 2 — `KISCircuitBreaker` state machine + `KISCircuitOpen` + module singleton (migration-0)

**Files:**
- Create `app/services/brokers/kis/circuit_breaker.py`.
- Test (extend) `tests/services/brokers/kis/test_circuit_breaker.py`.

**Interfaces:**
- `class KISCircuitOpen(Exception)` — raised on fail-fast; plain `Exception` subclass so existing broad `except Exception` fallbacks catch it. Carries `retry_after: float` (seconds until the next probe) for observability.
- `def is_kis_connect_failure(exc: BaseException) -> bool` — `True` iff `exc` is one of `httpx.ConnectTimeout`, `httpx.ConnectError`, `httpx.PoolTimeout`, `httpx.ReadTimeout`, or builtin `ConnectionRefusedError`. `ReadTimeout` is included because a maintenance-window LB accepts the TCP connection but the backend hangs, so the outage manifests as a *read* timeout, not a connect error (and `ReadTimeout`/`ConnectTimeout` are sibling `TimeoutException` subclasses, so listing only connect errors would never trip on a read-hang); the retry loop + N-consecutive threshold keep a lone slow query from tripping it. `httpx.WriteTimeout`, `httpx.HTTPStatusError`, `RateLimitExceededError`, and KIS business `RuntimeError` return `False` (KIS reached).
- `class KISCircuitBreaker` — `__init__(self, *, now: Callable[[], float] = time.monotonic, settings_obj: Any = settings)`. Methods: `before_request() -> None` (sync; raises `KISCircuitOpen` when open pre-cooldown or when a half-open probe is already in flight), `record_success() -> None`, `record_failure() -> None`, `record_reachable_error() -> None`, `reset() -> None`, plus read-only `state` property (`"closed" | "open" | "half_open"`) and `failure_count`.
- Module singleton: `def get_kis_circuit_breaker() -> KISCircuitBreaker`, `def reset_kis_circuit_breaker() -> None`.

Steps:

- [ ] **Write the failing tests — full state machine, deterministic.** Append to `tests/services/brokers/kis/test_circuit_breaker.py`:
```python
import httpx

from app.core.async_rate_limiter import RateLimitExceededError
from app.services.brokers.kis import circuit_breaker as cb
from app.services.brokers.kis.circuit_breaker import (
    KISCircuitBreaker,
    KISCircuitOpen,
    is_kis_connect_failure,
)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeSettings:
    def __init__(self, *, enabled=True, threshold=3, cooldown=45) -> None:
        self.kis_circuit_breaker_enabled = enabled
        self.kis_circuit_breaker_failure_threshold = threshold
        self.kis_circuit_breaker_cooldown_seconds = cooldown


@pytest.fixture(autouse=True)
def _reset_singleton():
    cb.reset_kis_circuit_breaker()
    yield
    cb.reset_kis_circuit_breaker()


def _make(**kw):
    clock = _Clock()
    breaker = KISCircuitBreaker(now=clock.now, settings_obj=_FakeSettings(**kw))
    return breaker, clock


class TestConnectClassifier:
    @pytest.mark.parametrize(
        "exc",
        [httpx.ConnectTimeout(""), httpx.ConnectError(""), httpx.PoolTimeout(""),
         httpx.ReadTimeout(""), ConnectionRefusedError()],
    )
    def test_connect_failures_classified(self, exc):
        assert is_kis_connect_failure(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [httpx.WriteTimeout(""),
         httpx.HTTPStatusError("x", request=None, response=None),
         RateLimitExceededError("throttle"), RuntimeError("KIS business error"),
         ValueError("boom")],
    )
    def test_non_connect_not_classified(self, exc):
        # WriteTimeout stays OUT of the trip set; ReadTimeout is IN (read-hang
        # outage) and is asserted in test_connect_failures_classified above.
        assert is_kis_connect_failure(exc) is False


class TestStateMachine:
    def test_closed_passthrough_until_threshold(self):
        breaker, _ = _make(threshold=3)
        breaker.before_request()          # closed -> no raise
        breaker.record_failure()          # 1
        breaker.record_failure()          # 2
        assert breaker.state == "closed"
        breaker.before_request()          # still closed
        breaker.record_failure()          # 3 -> open
        assert breaker.state == "open"

    def test_success_resets_failure_count(self):
        breaker, _ = _make(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.failure_count == 0
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed"   # not opened: counter was reset

    def test_reachable_error_does_not_trip(self):
        breaker, _ = _make(threshold=2)
        breaker.record_reachable_error()   # 429/business — must not count
        breaker.record_reachable_error()
        breaker.record_reachable_error()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_open_raises_with_zero_side_effects_until_cooldown(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()           # -> open
        assert breaker.state == "open"
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()
        clock.advance(44.9)
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()

    def test_cooldown_transitions_to_half_open_single_probe(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()           # hands out THE probe -> half_open
        assert breaker.state == "half_open"
        # concurrent burst: every other half-open caller fails fast (no stampede)
        for _ in range(5):
            with pytest.raises(KISCircuitOpen):
                breaker.before_request()

    def test_probe_success_closes(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()
        breaker.record_success()
        assert breaker.state == "closed"
        breaker.before_request()           # closed again -> no raise

    def test_probe_reachable_error_closes(self):
        # A 429 during the probe proves KIS is reachable -> close.
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()
        breaker.record_reachable_error()
        assert breaker.state == "closed"

    def test_probe_failure_reopens_and_extends_cooldown(self):
        breaker, clock = _make(threshold=1, cooldown=45)
        breaker.record_failure()
        clock.advance(45)
        breaker.before_request()           # half_open probe
        breaker.record_failure()           # probe failed -> reopen
        assert breaker.state == "open"
        with pytest.raises(KISCircuitOpen):
            breaker.before_request()        # cooldown restarted from now
        clock.advance(45)
        breaker.before_request()            # probe again
        assert breaker.state == "half_open"

    def test_disabled_is_complete_no_op(self):
        breaker, _ = _make(enabled=False, threshold=1)
        for _ in range(10):
            breaker.record_failure()
        breaker.before_request()            # never raises
        assert breaker.state == "closed"

    def test_open_logs_warning_close_logs_info(self, caplog):
        import logging
        breaker, clock = _make(threshold=1, cooldown=1)
        with caplog.at_level(logging.INFO):
            breaker.record_failure()        # open -> WARNING
            clock.advance(1)
            breaker.before_request()        # half_open -> INFO
            breaker.record_success()        # close -> INFO
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "open" in text.lower()


class TestSingleton:
    def test_shared_instance(self):
        assert cb.get_kis_circuit_breaker() is cb.get_kis_circuit_breaker()

    def test_reset_drops_instance(self):
        first = cb.get_kis_circuit_breaker()
        cb.reset_kis_circuit_breaker()
        assert cb.get_kis_circuit_breaker() is not first
```

- [ ] **Run it — fails.** `uv run pytest tests/services/brokers/kis/test_circuit_breaker.py -v`
  Expected: `ModuleNotFoundError: app.services.brokers.kis.circuit_breaker`.

- [ ] **Minimal impl — create the module.** Create `app/services/brokers/kis/circuit_breaker.py`:
```python
"""ROB-699 — per-process in-process circuit breaker for the shared KIS client.

Closed = pure passthrough. After N consecutive transport connect-failures (KIS
host unreachable, e.g. maintenance) the breaker OPENS and ``before_request``
raises ``KISCircuitOpen`` immediately — zero HTTP, zero rate-limit wait — so the
existing /invest KIS→Toss→snapshot fallbacks fire in ~0ms. After a cooldown it
half-opens, hands out EXACTLY ONE probe, and closes on success (or re-opens on a
fresh connect-failure). State is a MODULE-LEVEL singleton shared across every
KISClient/BaseKISClient instance in the process (no Redis).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"

# Transport failures that mean "KIS host is unreachable / hung" and should trip
# the breaker. Includes ReadTimeout: during a maintenance window the KIS load
# balancer often ACCEPTS the TCP connection (so no ConnectError/ConnectTimeout)
# but the backend is down, so every request hangs until the read timeout — that
# is exactly the sustained-outage case the breaker exists to fail-fast. httpx
# raises ReadTimeout and ConnectTimeout as SIBLING TimeoutException subclasses
# (ReadTimeout is NOT a subclass of ConnectTimeout), so a set that lists only
# connect errors would NEVER trip on a read-hang outage. A SINGLE slow query is
# absorbed by the retry loop (ROB-270) and by the N-consecutive threshold, so a
# lone ReadTimeout does not open the breaker. WriteTimeout is intentionally left
# out (request-body write hang is not an outage signal on the read hot path).
# 429 / HTTPStatusError / business RuntimeError are "reachable" and never trip.
_CONNECT_FAILURES: tuple[type[BaseException], ...] = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    ConnectionRefusedError,
)


class KISCircuitOpen(Exception):
    """Fail-fast signal raised while the KIS circuit is open.

    Plain ``Exception`` subclass on purpose: the existing broad ``except
    Exception`` fallbacks (invest_quote_service per-symbol fetch, PriceFallback
    _apply_layer, KISHomeReader.fetch) catch it with no new wiring.
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"KIS circuit open — failing fast (retry in ~{retry_after:.1f}s)"
        )


def is_kis_connect_failure(exc: BaseException) -> bool:
    """True iff ``exc`` is a transport connect-failure that should trip the breaker."""
    return isinstance(exc, _CONNECT_FAILURES)


class KISCircuitBreaker:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        settings_obj: Any = settings,
    ) -> None:
        self._now = now
        self._settings = settings_obj
        self._state = _CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    # --- config (read lazily so the flag / test overrides are always live) ---
    @property
    def _enabled(self) -> bool:
        return bool(getattr(self._settings, "kis_circuit_breaker_enabled", True))

    @property
    def _threshold(self) -> int:
        return int(getattr(self._settings, "kis_circuit_breaker_failure_threshold", 5))

    @property
    def _cooldown(self) -> float:
        return float(getattr(self._settings, "kis_circuit_breaker_cooldown_seconds", 45))

    # --- introspection ---
    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    def reset(self) -> None:
        self._state = _CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    # --- gate (SYNCHRONOUS: no await between check and set -> single probe) ---
    def before_request(self) -> None:
        if not self._enabled:
            return
        if self._state == _OPEN:
            elapsed = self._now() - self._opened_at
            if elapsed < self._cooldown:
                raise KISCircuitOpen(self._cooldown - elapsed)
            # cooldown elapsed -> half-open, hand out THIS one probe
            self._state = _HALF_OPEN
            self._probe_in_flight = True
            logger.info("KIS circuit half-open: allowing one probe request")
            return
        if self._state == _HALF_OPEN:
            if self._probe_in_flight:
                raise KISCircuitOpen(self._cooldown)  # stampede guard
            self._probe_in_flight = True
            return
        # CLOSED
        return

    # --- outcomes ---
    def record_success(self) -> None:
        if not self._enabled:
            return
        if self._state == _HALF_OPEN:
            logger.info("KIS circuit closed: probe succeeded")
        self._state = _CLOSED
        self._failures = 0
        self._probe_in_flight = False

    def record_reachable_error(self) -> None:
        """KIS responded (429 / HTTPStatusError / business RuntimeError / rate-limit
        exhausted). Proves reachability: never trips, and closes a half-open probe."""
        if not self._enabled:
            return
        if self._state == _HALF_OPEN:
            logger.info("KIS circuit closed: probe reached KIS (non-2xx)")
            self._state = _CLOSED
            self._failures = 0
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if not self._enabled:
            return
        self._probe_in_flight = False
        if self._state == _HALF_OPEN:
            self._opened_at = self._now()
            self._state = _OPEN
            logger.warning("KIS circuit re-opened: probe connect-failure")
            return
        self._failures += 1
        if self._state == _CLOSED and self._failures >= self._threshold:
            self._opened_at = self._now()
            self._state = _OPEN
            logger.warning(
                "KIS circuit OPEN after %d consecutive connect-failures; "
                "failing fast for %.0fs",
                self._failures,
                self._cooldown,
            )


_breaker: KISCircuitBreaker | None = None


def get_kis_circuit_breaker() -> KISCircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = KISCircuitBreaker()
    return _breaker


def reset_kis_circuit_breaker() -> None:
    global _breaker
    _breaker = None
```

- [ ] **Run it — passes.** `uv run pytest tests/services/brokers/kis/test_circuit_breaker.py -v` → all pass.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-699): KISCircuitBreaker state machine + KISCircuitOpen + module singleton"`

---

## Task 3 — Wire the breaker into the KIS dispatch (migration-0)

**Files:**
- Modify `app/services/brokers/kis/base.py` — import the breaker (near `describe_exception` import, `:20`); extract the current body of `_request_with_rate_limit_with_headers` (`:453`–`:570`) verbatim into a new coroutine `_dispatch_rate_limited_with_headers` with the identical parameter list; replace the original method body with the breaker wrapper.
- Test (create) `tests/test_kis_circuit_breaker_dispatch.py`.

**Interfaces:**
- New private `async def _dispatch_rate_limited_with_headers(self, method, url, *, headers, params=None, json_body=None, timeout=5.0, api_name="unknown", tr_id=None, retry_request_errors=True, max_retries_override=None) -> tuple[dict[str, Any], dict[str, str]]` — the today's dispatch, byte-for-byte.
- `_request_with_rate_limit_with_headers(...)` keeps its **exact** public signature (`base.py:427`) and return type; it now calls `get_kis_circuit_breaker().before_request()` first, then the extracted dispatch inside a `try/except BaseException` that calls `record_failure()` (connect) / `record_reachable_error()` (else) before re-raising, and `record_success()` on the normal return. `_request_with_rate_limit` (`:375`) is unchanged (still delegates).

Steps:

- [ ] **Write the failing tests — breaker behavior at the dispatch seam.** Create `tests/test_kis_circuit_breaker_dispatch.py`:
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
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 0  # 1 attempt — fail fast per call in tests
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    # breaker knobs (read via the injected settings_obj on the breaker, below)


class _FakeClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FakeSettings()


class _BreakerSettings:
    kis_circuit_breaker_enabled = True
    kis_circuit_breaker_failure_threshold = 3
    kis_circuit_breaker_cooldown_seconds = 45


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture(autouse=True)
def _install_breaker(clock):
    # Inject a deterministic-clock breaker as THE process singleton.
    cb._breaker = KISCircuitBreaker(now=clock.now, settings_obj=_BreakerSettings())
    yield
    cb.reset_kis_circuit_breaker()


def _client_with(execute):
    client = _FakeClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    client._get_limiter = AsyncMock(return_value=limiter)  # type: ignore[method-assign]
    client._ensure_client = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    client._execute_http_request = execute  # type: ignore[method-assign]
    return client, limiter


async def _call(client):
    return await client._request_with_rate_limit_with_headers(
        "GET", "https://host/uapi/x", headers={}, api_name="inquire_price"
    )


@pytest.mark.asyncio
async def test_connect_failures_open_the_breaker():
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"


@pytest.mark.asyncio
async def test_open_breaker_zero_http_zero_wait():
    execute = AsyncMock(side_effect=httpx.ConnectError(""))
    client, limiter = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"
    execute.reset_mock()
    limiter.acquire.reset_mock()
    with pytest.raises(KISCircuitOpen):
        await _call(client)
    execute.assert_not_awaited()        # ZERO HTTP
    limiter.acquire.assert_not_awaited()  # ZERO rate-limit wait


@pytest.mark.asyncio
async def test_429_response_does_not_open_breaker():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "초과"}
    execute = AsyncMock(return_value=resp)
    client, _ = _client_with(execute)
    for _ in range(6):
        await _call(client)  # KIS-reachable throttle body, returned not raised
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_success_returns_and_keeps_closed():
    ok = MagicMock()
    ok.status_code = 200
    ok.headers = {}
    ok.json.return_value = {"rt_cd": "0", "output": []}
    client, _ = _client_with(AsyncMock(return_value=ok))
    data, _headers = await _call(client)
    assert data["rt_cd"] == "0"
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_cooldown_half_open_probe_closes_on_success(clock):
    ok = MagicMock()
    ok.status_code = 200
    ok.headers = {}
    ok.json.return_value = {"rt_cd": "0", "output": []}
    execute = AsyncMock(side_effect=[httpx.ConnectTimeout("")] * 3 + [ok])
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    assert cb.get_kis_circuit_breaker().state == "open"
    clock.advance(45)
    data, _headers = await _call(client)  # the single probe -> success -> closed
    assert data["rt_cd"] == "0"
    assert cb.get_kis_circuit_breaker().state == "closed"


@pytest.mark.asyncio
async def test_disabled_flag_is_complete_passthrough():
    class _Disabled(_BreakerSettings):
        kis_circuit_breaker_enabled = False

    cb._breaker = KISCircuitBreaker(settings_obj=_Disabled())
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, limiter = _client_with(execute)
    for _ in range(10):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    # never opens; every call still reached the dispatch (limiter acquired)
    assert cb.get_kis_circuit_breaker().state == "closed"
    assert limiter.acquire.await_count == 10


@pytest.mark.asyncio
async def test_open_stampede_does_not_close_circuit(clock):
    # Locks the "before_request() outside the try/except" invariant: once a
    # half-open probe is in flight, a concurrent caller must raise KISCircuitOpen
    # WITHOUT that raise being reclassified as a reachable error (which would
    # wrongly close the still-probing circuit).
    execute = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    client, _ = _client_with(execute)
    for _ in range(3):
        with pytest.raises(httpx.ConnectTimeout):
            await _call(client)
    breaker = cb.get_kis_circuit_breaker()
    assert breaker.state == "open"
    clock.advance(45)
    breaker.before_request()          # hands out THE probe -> half_open, in flight
    assert breaker.state == "half_open"
    with pytest.raises(KISCircuitOpen):
        await _call(client)           # stampede caller: must fail-fast
    assert breaker.state == "half_open"  # still probing, NOT closed
```

- [ ] **Run it — fails.** `uv run pytest tests/test_kis_circuit_breaker_dispatch.py -v`
  Expected: `test_connect_failures_open_the_breaker`, `..._zero_http_zero_wait`, `..._half_open_probe...` FAIL (breaker never opens — dispatch has no breaker calls yet; `KISCircuitOpen` never raised). The 429 / success / disabled tests pass coincidentally (closed passthrough). Keep them to lock the boundaries.

- [ ] **Minimal impl part A — import + extract.** In `app/services/brokers/kis/base.py`, add the import next to line 20:
```python
from app.services.brokers.kis.circuit_breaker import (
    get_kis_circuit_breaker,
    is_kis_connect_failure,
)
```
Rename the current method `_request_with_rate_limit_with_headers` (`:427`) to `_dispatch_rate_limited_with_headers`, keeping its **entire body (`:453`–`:570`) and parameter list byte-for-byte unchanged** (only the `def` name and docstring change; the docstring can stay). Do NOT alter the retry loop, the 429 handling, the `RateLimitExceededError` raise, or `describe_exception` usage.

- [ ] **Minimal impl part B — the wrapper.** Add a new `_request_with_rate_limit_with_headers` with the ORIGINAL public signature (`:427`) that wraps the dispatch:
```python
    async def _request_with_rate_limit_with_headers(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
        retry_request_errors: bool = True,
        max_retries_override: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """ROB-699 — breaker-guarded wrapper over the KIS dispatch.

        When the per-process circuit is OPEN this raises ``KISCircuitOpen`` before
        any rate-limit wait or HTTP call, so /invest KIS→Toss fallbacks fire in
        ~0ms. Closed = pure passthrough. See ``circuit_breaker.py``.
        """
        breaker = get_kis_circuit_breaker()
        breaker.before_request()  # raises KISCircuitOpen when open — 0 HTTP, 0 wait
        try:
            result = await self._dispatch_rate_limited_with_headers(
                method,
                url,
                headers=headers,
                params=params,
                json_body=json_body,
                timeout=timeout,
                api_name=api_name,
                tr_id=tr_id,
                retry_request_errors=retry_request_errors,
                max_retries_override=max_retries_override,
            )
        except BaseException as exc:  # noqa: BLE001 — classify then re-raise unchanged
            if is_kis_connect_failure(exc):
                breaker.record_failure()
            else:
                breaker.record_reachable_error()
            raise
        breaker.record_success()
        return result
```
`_request_with_rate_limit` (`:375`) is unchanged — it still calls `_request_with_rate_limit_with_headers`, now transparently breaker-guarded.

**Critical invariant:** `breaker.before_request()` MUST stay **outside** the `try/except BaseException`. If a refactor moves it inside, a HALF_OPEN stampede `KISCircuitOpen` gets caught, classified as a "reachable" error (`is_kis_connect_failure(KISCircuitOpen)` is `False`), and `record_reachable_error()` would wrongly CLOSE the still-probing circuit. The added `test_open_stampede_does_not_close_circuit` (below) locks this.

- [ ] **Run it — passes.** `uv run pytest tests/test_kis_circuit_breaker_dispatch.py -v` → all pass.

- [ ] **Global test isolation — kill the singleton leak BEFORE running the legacy suite.** The breaker is a per-process module singleton and ships **enabled by default**, so every existing KIS test that raises a transport error through `_request_with_rate_limit_with_headers` now increments the SHARED breaker. Legacy suites do this a lot (`tests/test_kis_request_error_retry_policy.py` raises `httpx.ReadTimeout`/`RequestError` 9×, `tests/test_kis_base_rate_limit.py` 8×, `tests/test_kis_order_no_double_submit.py` 3×). With no reset the counter crosses `threshold=5` across tests, so a later test that raises a connect/read error and asserts `pytest.raises(httpx.RequestError)` instead gets `KISCircuitOpen` → order-dependent flake. **Per-test resets alone do NOT fix it** — a single legacy test can itself issue ≥5 failing dispatch calls, tripping mid-test. So add an **autouse global fixture** in `tests/conftest.py` that both resets the singleton and forces the breaker OFF for every test (`Settings` is runtime-mutable — see `base.py:297` — so `monkeypatch.setattr` works and auto-restores):
```python
@pytest.fixture(autouse=True)
def _isolate_kis_circuit_breaker(monkeypatch):
    # ROB-699: the KIS circuit breaker is a per-process singleton, enabled by
    # default. Force it OFF + reset it for every test so the existing KIS suite
    # is byte-identical passthrough and no connect/read errors leak across tests.
    # Breaker tests inject their own enabled breaker (settings_obj / cb._breaker),
    # which ignores this global flag.
    from app.core.config import settings
    from app.services.brokers.kis import circuit_breaker as _cb

    monkeypatch.setattr(settings, "kis_circuit_breaker_enabled", False, raising=False)
    _cb.reset_kis_circuit_breaker()
    yield
    _cb.reset_kis_circuit_breaker()
```
  Fixture ordering: this conftest-level autouse fixture runs BEFORE the module-level `_install_breaker`/`_reset_singleton` fixtures in the new breaker test files, so those files' injected enabled breaker (its own `settings_obj`) wins for their own tests while every other test sees a disabled no-op.

- [ ] **Regression — the whole KIS base/dispatch surface stays green.** `uv run pytest tests/test_kis_base_rate_limit.py tests/test_kis_request_error_retry_policy.py tests/test_kis_order_no_double_submit.py tests/test_kis_domestic_orders_retry.py tests/test_kis_overseas_orders_retry.py -v`
  Expected: all pass unchanged — the `tests/conftest.py` fixture forces the breaker OFF, so the wrapper is a pure passthrough and connect/read failures re-raise exactly as before (no breaker state, no cross-test leak).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-699): guard KIS dispatch with the circuit breaker (fail-fast on connect outage)"`

---

## Task 4 — Fallback propagation regression + operator runbook (test-only + docs, migration-0)

**Files:**
- Test (create) `tests/test_invest_price_fallback_circuit_open.py` — proves `KISCircuitOpen` flows through the EXISTING broad-`except` fallbacks with no source change.
- Create `docs/runbooks/kis-circuit-breaker.md`.
- **No source change to any caller** — `PriceFallbackResolver` (`invest_price_fallback.py:59`), `InvestQuoteService` (`invest_quote_service.py:108`/`:125`), `KISHomeReader` (`invest_home_readers.py:301`) already catch `Exception`.

**Interfaces:** none produced. Consumes `PriceFallbackResolver.resolve` and `KISCircuitOpen`.

Steps:

- [ ] **Write the regression test — KISCircuitOpen fails through KIS layer to Toss.** Create `tests/test_invest_price_fallback_circuit_open.py`:
```python
from __future__ import annotations

import pytest

from app.services.brokers.kis.circuit_breaker import KISCircuitOpen
from app.services.invest_price_fallback import PriceFallbackResolver

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def test_kis_circuit_open_is_plain_exception():
    # Must be a plain Exception subclass so existing broad handlers catch it.
    assert issubclass(KISCircuitOpen, Exception)
    assert not issubclass(KISCircuitOpen, BaseException) or issubclass(
        KISCircuitOpen, Exception
    )


async def test_open_circuit_kis_layer_fails_through_to_toss():
    async def kis_fetch(symbols):
        raise KISCircuitOpen(45.0)  # breaker open -> ~0ms raise

    async def toss_fetch(symbols):
        return {s: 100.0 for s in symbols}

    async def snapshot_fetch(symbols):
        return {}

    resolver = PriceFallbackResolver(
        kis_fetch=kis_fetch,
        toss_fetch=toss_fetch,
        snapshot_fetch=snapshot_fetch,
        market="kr",
    )
    out = await resolver.resolve(["005930", "000660"])
    # KIS layer raised KISCircuitOpen; _apply_layer caught it fail-open; Toss filled.
    assert out == {"005930": 100.0, "000660": 100.0}


async def test_open_circuit_falls_to_snapshot_when_toss_also_empty():
    async def kis_fetch(symbols):
        raise KISCircuitOpen(45.0)

    async def toss_fetch(symbols):
        return {}

    async def snapshot_fetch(symbols):
        return {s: 42.0 for s in symbols}

    resolver = PriceFallbackResolver(
        kis_fetch=kis_fetch,
        toss_fetch=toss_fetch,
        snapshot_fetch=snapshot_fetch,
        market="us",
    )
    out = await resolver.resolve(["AAPL"])
    assert out == {"AAPL": 42.0}
```

- [ ] **Run it — passes on first run (contract lock).** `uv run pytest tests/test_invest_price_fallback_circuit_open.py -v`
  This is a characterization/regression test: it is GREEN immediately because the fallbacks already catch broad `Exception`. Its job is to LOCK the "no new fallback wiring" guarantee — if a future refactor narrows an `except` and stops catching `KISCircuitOpen`, this test goes red. (If it is unexpectedly red now, that means a caller does NOT catch `Exception` — stop and reconcile, do not add wiring blindly.)

- [ ] **Create the operator runbook.** Create `docs/runbooks/kis-circuit-breaker.md` documenting: what trips it (KIS transport connect/read-hang failures: `ConnectTimeout`/`ConnectError`/`PoolTimeout`/`ReadTimeout`/`ConnectionRefusedError` — NOT 429/business/`WriteTimeout`); why `ReadTimeout` is in the trip set (a maintenance LB accepts the TCP connection then hangs, so the outage shows up as a read timeout, not a connect error); defaults (`kis_circuit_breaker_enabled=True`, `failure_threshold=5`, `cooldown_seconds=45`); how to disable in an incident (`KIS_CIRCUIT_BREAKER_ENABLED=false` → complete passthrough); the log lines to grep (`WARNING "KIS circuit OPEN after N consecutive connect-failures"`, `INFO "KIS circuit half-open"`, `INFO "KIS circuit closed"`); and the scope notes (mock + live share one breaker; the OAuth token endpoint is not guarded; the first /invest load after an outage still pays the in-flight concurrent burst, only subsequent loads fail-fast).

- [ ] **Full regression sweep.** `uv run pytest tests/services/brokers/kis/test_circuit_breaker.py tests/test_kis_circuit_breaker_dispatch.py tests/test_invest_price_fallback_circuit_open.py tests/test_invest_price_fallback.py tests/test_invest_home_readers.py -v`
  Expected: all pass — breaker suites green, ROB-696 fallback + home-reader suites unaffected.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "test(ROB-699): lock KISCircuitOpen fallback propagation + add operator runbook"`

---

## Rollout / verification notes (non-blocking)

- **Defaults are estimates.** `failure_threshold=5` / `cooldown_seconds=45` are seeded from the Sentry incident (~5s connect timeout × retries, 27.5s home loads). Tune from production after rollout; both are env-overridable with zero deploy.
- **`ReadTimeout` is in the trip set on purpose.** The Sentry evidence points to connect timeouts, but the timeout float applies equally to connect and read, and a maintenance LB that accepts the TCP connection then hangs surfaces as `ReadTimeout` — excluding it would leave the breaker unable to trip for that (very plausible) outage shape. The `threshold=5`-consecutive guard + KIS→Toss fail-open downstream make a false trip on genuinely-slow-but-up KIS low-cost (Toss serves during the cooldown). If production shows benign read-slowness tripping the breaker, raise the threshold or split connect-vs-read timeouts before dropping `ReadTimeout`.
- **First-load caveat is intended.** A `/invest` load fires ~24 KIS calls concurrently; they all pass the closed-breaker `before_request` gate before the 5th failure opens it, so the FIRST post-outage load still times out. Only SUBSEQUENT loads fail-fast. This is the correct per-process behavior — the breaker is a steady-state guard, not a first-request guard.
- **Mock ↔ live coupling.** One module singleton is shared by live and KIS-mock clients (per the approved design). During real KIS maintenance both hosts are down together; a live-only outage would also fail-fast mock calls (and vice-versa). Revisit only if false-coupling is observed in practice.
