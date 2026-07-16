"""ROB-892 — distributed Redis gate for KIS mock (VTS) REST dispatch.

The official KIS mock (VTS, ``openapivts.koreainvestment.com``) enforces a
conservative account/app-key-scoped budget of **one admitted request per
second** for *every* REST call (orders AND reads, domestic AND overseas,
regardless of TR id / path). The previous ``VTSOrderPacer`` was (a) only
applied to place-order POSTs and (b) process-local, so independent
API/MCP/worker PIDs could still burst the shared credential budget.

This module implements the distributed replacement. Design invariants
(see ROB-892 spec):

* Enforced at the common actual-dispatch boundary in ``base.py``, not per
  MCP handler / order method.
* Authority is **Redis server ``TIME`` + atomic Lua** — never the host wall
  clock, never a future reservation, never a lock held during sleep or the
  full HTTP call. A "claim" merely records the last-admitted server
  timestamp; it is a token-bucket-style admit, not a held mutex. A claim
  lost to cancellation/crash is **not** released or recycled into an order
  retry (conservative).
* Scope key is the exact mock host + a **non-reversible** sha256 fingerprint
  of the app key (reusing the safe ``_kis_mock_token_namespace`` pattern).
  Raw app key / secret / token / account never appear in the key, logs, or
  errors.
* Interval is ``1.0s`` plus a small configurable safety margin. TTL/cold
  start is deterministic.
* On contention the gate returns/computes ``retry-after`` and sleeps without
  spinning, retrying the atomic claim until a bounded deadline. Cancellation
  while waiting means HTTP=0.
* **Fail-closed:** Redis error/timeout, a malformed Lua result, or an
  acquire-deadline is a stable pre-dispatch failure (raises
  ``DistributedGateUnavailable``). Mutations prove HTTP=0 and never fall
  back to local or unthrottled HTTP. Reads cannot bypass the gate during a
  Redis outage because they consume the same quota and fail the same way.
* Live requests never call this gate (the dispatch boundary short-circuits
  when the request host is not the VTS host).

Dispatch ordering preserved by ``BaseKISClient``:

    prepare token / client / local limiter
      -> wait for distributed availability (contention sleep)
      -> run order ``pre_send_hook`` (freshness)
      -> atomically claim immediately before dispatch
      -> mark dispatched + start HTTP (no further unbounded wait)

If another PID wins the claim, the gate waits and **reruns freshness** on
the next claim attempt; no long gate wait follows the final freshness check.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# One admitted request per second is the official KIS mock account/app-key
# budget. The safety margin absorbs Redis/server clock skew so two adjacent
# admits can never land within the real 1.0s window.
_DEFAULT_INTERVAL_SECONDS = 1.0
_DEFAULT_SAFETY_MARGIN_SECONDS = 0.05
# Cooldown timestamp TTL: bounds cold-start ambiguity and lets a crashed
# scope recover deterministically without operator intervention. Long enough
# to survive a Redis EVENTUX/failover tick, short enough to avoid a stuck
# scope after a credential rotation.
_DEFAULT_TTL_SECONDS = 120
_DEFAULT_SOCKET_TIMEOUT_SECONDS = 2.0
_DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS = 2.0
# Bounded deadline for acquiring a permit under contention. Generous enough
# for a small backlog to drain, short enough that a mutation fails closed
# instead of hanging the caller.
_DEFAULT_ACQUIRE_DEADLINE_SECONDS = 10.0

# Redis key prefix is intentionally distinct from the OAuth token namespace
# (``kis_mock:{host}:{fp}``) so the gate state never collides with token
# cache state for the same credential scope.
_KEY_PREFIX = "kis_mock:gate"

# Atomic claim. Uses Redis server TIME (never the host clock). Returns a
# two-element array: ``{admitted(1/0), retry_after_ms}``. ``admitted=1``
# means THIS caller recorded the last-admit timestamp and may dispatch;
# ``admitted=0`` means another caller owns the current slot and the caller
# must sleep ``retry_after_ms`` and retry. The key TTL is refreshed on every
# claim (admitted or not) so a waiter never sees a stale/missing cooldown.
_CLAIM_LUA = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local interval_ms = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])
local last = redis.call('GET', KEYS[1])
if not last then
    redis.call('SET', KEYS[1], tostring(now_ms), 'PX', ttl_ms)
    return {1, 0}
end
last = tonumber(last)
local eligible_at = last + interval_ms
if now_ms >= eligible_at then
    redis.call('SET', KEYS[1], tostring(now_ms), 'PX', ttl_ms)
    return {1, 0}
end
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {0, eligible_at - now_ms}
"""


class DistributedGateUnavailable(RuntimeError):
    """Stable pre-dispatch failure: Redis unreachable/timeout/malformed, or
    the acquire deadline elapsed.

    Raised *before* any HTTP mutation, so callers can prove HTTP=0. Mutations
    must surface this as NOT_CREATED-equivalent and never fall back to local
    or unthrottled HTTP.
    """


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of a successful gate acquisition (for logging/metrics only)."""

    scope_key: str
    waited_seconds: float
    contention_attempts: int


FreshnessHook = Callable[[], Awaitable[None]]


def _app_key_fingerprint(app_key: str) -> str:
    """Non-reversible 16-hex-char fingerprint, matching the token namespace."""
    return hashlib.sha256(app_key.encode()).hexdigest()[:16]


def build_vts_gate_scope_key(*, host: str, app_key: str) -> str:
    """Build the distributed-gate scope key for one VTS credential scope.

    ``host`` MUST be the normalized ``netloc`` (``host[:port]``) of the mock
    base URL, lower-cased. ``app_key`` is the raw mock app key; only its
    sha256 fingerprint appears in the key.
    """
    normalized = (host or "").lower()
    if not normalized:
        raise ValueError("VTS gate scope requires a non-empty host")
    if not app_key:
        # An empty app key cannot identify a credential scope; fail loudly
        # rather than silently collapsing every empty-key caller into one.
        raise ValueError("VTS gate scope requires a non-empty app key")
    return f"{_KEY_PREFIX}:{normalized}:{_app_key_fingerprint(app_key)}"


def netloc_from_url(url: str) -> str:
    """Extract the lower-cased ``netloc`` (host[:port]) from an absolute URL."""
    return urlsplit(url).netloc.lower()


class VTSDistributedGate:
    """Redis-backed distributed admit gate for KIS mock (VTS) REST calls.

    A single process-wide instance is shared by every mock dispatch path
    (see ``get_vts_distributed_gate``). The instance is cheap to construct
    and only contacts Redis inside ``acquire``; live requests never call it.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        safety_margin_seconds: float = _DEFAULT_SAFETY_MARGIN_SECONDS,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        socket_timeout_seconds: float = _DEFAULT_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout_seconds: float = _DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS,
        acquire_deadline_seconds: float = _DEFAULT_ACQUIRE_DEADLINE_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if safety_margin_seconds < 0:
            raise ValueError("safety_margin_seconds must be non-negative")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if acquire_deadline_seconds <= 0:
            raise ValueError("acquire_deadline_seconds must be positive")

        self._redis_url = redis_url
        self._interval_seconds = float(interval_seconds)
        self._safety_margin_seconds = float(safety_margin_seconds)
        self._interval_with_margin_ms = int(
            round((self._interval_seconds + self._safety_margin_seconds) * 1000)
        )
        self._ttl_ms = int(ttl_seconds) * 1000
        self._socket_timeout_seconds = float(socket_timeout_seconds)
        self._socket_connect_timeout_seconds = float(socket_connect_timeout_seconds)
        self._acquire_deadline_seconds = float(acquire_deadline_seconds)
        self._redis: redis.Redis | None = None
        self._redis_lock = asyncio.Lock()
        self._claim_sha: str | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def safety_margin_seconds(self) -> float:
        return self._safety_margin_seconds

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            async with self._redis_lock:
                if self._redis is None:
                    self._redis = redis.from_url(
                        self._redis_url,
                        socket_timeout=self._socket_timeout_seconds,
                        socket_connect_timeout=self._socket_connect_timeout_seconds,
                        decode_responses=True,
                    )
        assert self._redis is not None
        return self._redis

    async def close(self) -> None:
        """Close the Redis connection. Safe to call multiple times."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            self._redis = None

    async def _claim(self, scope_key: str) -> tuple[bool, float]:
        """Run the atomic Lua claim. Returns ``(admitted, retry_after_seconds)``.

        Any Redis error, timeout, or malformed result is translated into
        ``DistributedGateUnavailable`` (fail-closed, HTTP=0).
        """
        try:
            client = await self._get_redis()
            # EVAL every call (no EVALSHA cache) so the disposable test server
            # and a fresh prod Redis both work without a NOSCRIPT retry path.
            raw = await client.execute_command(
                "EVAL",
                _CLAIM_LUA,
                1,
                scope_key,
                str(self._interval_with_margin_ms),
                str(self._ttl_ms),
            )
        except Exception as exc:  # noqa: BLE001 — every Redis failure is fail-closed
            raise DistributedGateUnavailable(
                f"VTS distributed gate Redis claim failed for scope {scope_key!r}: {exc}"
            ) from exc

        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise DistributedGateUnavailable(
                f"VTS distributed gate returned malformed result for scope "
                f"{scope_key!r}: {raw!r}"
            )
        try:
            admitted = bool(int(raw[0]))
            retry_after_ms = int(raw[1])
        except (TypeError, ValueError) as exc:
            raise DistributedGateUnavailable(
                f"VTS distributed gate returned non-numeric result for scope "
                f"{scope_key!r}: {raw!r}"
            ) from exc
        return admitted, max(0.0, retry_after_ms / 1000.0)

    async def acquire(
        self,
        scope_key: str,
        *,
        freshness_hook: FreshnessHook | None = None,
        deadline_monotonic: float | None = None,
        call_class: str = "unknown",
    ) -> AcquireResult:
        """Block until a distributed permit is admitted for ``scope_key``.

        Ordering: coarse contention wait -> run ``freshness_hook`` -> atomic
        claim immediately before dispatch. On contention (another PID won the
        current slot), sleep ``retry-after`` and **rerun freshness** on the
        next claim attempt. Returns once the atomic claim succeeded — the
        caller must then mark dispatched and start HTTP with no further
        unbounded wait.

        Args:
            scope_key: credential/host scope (see ``build_vts_gate_scope_key``).
            freshness_hook: optional mock pre-send freshness callback, rerun
                before every claim attempt so a stale book is never POSTed.
            deadline_monotonic: absolute ``time.monotonic()`` deadline. When
                ``None`` the configured ``acquire_deadline_seconds`` budget is
                used (measured from the first claim attempt).
            call_class: safe label (e.g. ``"order"``, ``"read"``) for logs.

        Raises:
            DistributedGateUnavailable: Redis error/timeout/malformed, or the
                acquire deadline elapsed (HTTP=0, no fallback).
            asyncio.CancelledError: re-raised if the wait is cancelled; the
                caller never started HTTP (HTTP=0).
        """
        start = time.monotonic()
        pid = os.getpid()
        if deadline_monotonic is None:
            deadline_monotonic = start + self._acquire_deadline_seconds

        attempts = 0
        last_fingerprint = scope_key.rsplit(":", 1)[-1]

        # Freshness is run BEFORE every claim attempt, including the first,
        # so the very first (likely-immediately-admissible) claim still
        # verifies the book right before the atomic admit.
        while True:
            if freshness_hook is not None:
                await freshness_hook()
            attempts += 1
            admitted, retry_after = await self._claim(scope_key)
            if admitted:
                waited = time.monotonic() - start
                if attempts > 1 or waited > 0.01:
                    logger.info(
                        "vts_gate admitted scope=%s fp=%s pid=%s class=%s "
                        "attempts=%d waited=%.3fs",
                        scope_key,
                        last_fingerprint,
                        pid,
                        call_class,
                        attempts,
                        waited,
                    )
                return AcquireResult(
                    scope_key=scope_key,
                    waited_seconds=waited,
                    contention_attempts=attempts,
                )

            # Contention: another PID won this slot. Sleep without spinning,
            # but never past the bounded deadline.
            now = time.monotonic()
            if now + retry_after > deadline_monotonic:
                raise DistributedGateUnavailable(
                    f"VTS distributed gate acquire deadline "
                    f"({self._acquire_deadline_seconds:.1f}s) exceeded for "
                    f"scope {scope_key!r} (class={call_class}, pid={pid})"
                )
            logger.info(
                "vts_gate contention scope=%s fp=%s pid=%s class=%s "
                "retry_after=%.3fs attempt=%d",
                scope_key,
                last_fingerprint,
                pid,
                call_class,
                retry_after,
                attempts,
            )
            await asyncio.sleep(retry_after)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_gate: VTSDistributedGate | None = None
_gate_lock = asyncio.Lock()


async def get_vts_distributed_gate() -> VTSDistributedGate:
    """Return the process-wide gate, lazily built from runtime settings.

    Only ``acquire`` contacts Redis; constructing the singleton is free, so
    live processes that never dispatch a mock call pay zero Redis cost.
    """
    global _gate
    if _gate is None:
        async with _gate_lock:
            if _gate is None:
                from app.core.config import settings  # local import avoids cycles

                _gate = VTSDistributedGate(
                    redis_url=settings.get_redis_url(),
                    interval_seconds=_DEFAULT_INTERVAL_SECONDS,
                    safety_margin_seconds=_DEFAULT_SAFETY_MARGIN_SECONDS,
                    ttl_seconds=_DEFAULT_TTL_SECONDS,
                    socket_timeout_seconds=_DEFAULT_SOCKET_TIMEOUT_SECONDS,
                    socket_connect_timeout_seconds=_DEFAULT_SOCKET_CONNECT_TIMEOUT_SECONDS,
                    acquire_deadline_seconds=_DEFAULT_ACQUIRE_DEADLINE_SECONDS,
                )
    return _gate


async def reset_vts_distributed_gate() -> None:
    """Discard the singleton (tests only). Closes any open Redis connection."""
    global _gate
    async with _gate_lock:
        if _gate is not None:
            await _gate.close()
        _gate = None
