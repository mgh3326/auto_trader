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
        return float(
            getattr(self._settings, "kis_circuit_breaker_cooldown_seconds", 45)
        )

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

    def release_probe(self) -> None:
        """Release a HALF_OPEN probe lease WITHOUT recording a KIS outcome.

        Call this when ``before_request`` handed out a probe but the call
        aborted BEFORE any HTTP reached KIS — a pre-send freshness block
        (``PreSendFreshnessError``), a distributed-gate failure/deadline
        (``DistributedGateUnavailable``), or an ``asyncio.CancelledError``
        during the pre-dispatch wait. These are HTTP=0 pre-dispatch aborts:

        * they must NOT be mistaken for a KIS success/failure, so they do not
          change ``_state`` or the consecutive ``_failures`` count;
        * they only release the exclusive probe lease so the next request can
          probe again — otherwise a HALF_OPEN breaker with ``_probe_in_flight``
          stuck ``True`` raises ``KISCircuitOpen`` forever (ROB-892 merge
          blocker: a Redis outage during a half-open probe must not permanently
          stall every subsequent KIS request).
        """
        if not self._enabled:
            return
        if self._state == _HALF_OPEN:
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
