"""Finnhub market-events fetch helpers.

Kept under the service layer so ingestion does not import MCP tooling modules at
module import time. The Finnhub SDK/settings imports stay lazy so unit tests can
monkeypatch fetchers without requiring production credentials.

ROB-1384 — this shared calendar fetch previously had no per-attempt
timeout and no retry, so a single transient stall surfaced as a total
lookup failure (and downstream the session could not distinguish
"no earnings scheduled" from "lookup failed"). The fetch now mirrors
the ROB-510 news contract: a per-attempt timeout
(``FINNHUB_EARNINGS_TIMEOUT_S``) and bounded attempts
(``FINNHUB_EARNINGS_MAX_ATTEMPTS``) with a retryable-only classifier.
The 429 -> ``FinnhubQuotaExceededError`` contract is preserved exactly
and is never retried.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from typing import Any

import requests
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


class FinnhubQuotaExceededError(Exception):
    """Raised when Finnhub returns HTTP 429 (daily/per-minute quota exhausted).

    Callers should treat this as fail-closed: do not retry within the same run,
    and do not continue iterating remaining partitions.
    """

    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderWorkerStillRunningError(TimeoutError):
    """The provider worker outlived its attempt timeout plus the bounded
    drain grace and is still running (ROB-1384 R2-F2).

    Subclasses ``TimeoutError`` because the observable outcome for the
    caller is still "the attempt timed out" — but it is classified
    non-retryable: a new attempt would overlap a live provider call.
    """


DEFAULT_EARNINGS_TIMEOUT_S = 8.0
DEFAULT_EARNINGS_MAX_ATTEMPTS = 3

# ROB-1384 R2-F2: the drain after an attempt timeout is bounded — an
# unbounded wait would let a slow-drip response (read gaps shorter than
# the SDK read timeout) extend the deadline indefinitely. The bound is
# the call's own configured budget: each attempt may wait for its
# already-running worker until ``attempts * (per_attempt_timeout +
# EARNINGS_DRAIN_GRACE_S)`` measured from call start. A single-attempt
# call therefore returns within one grace of its deadline, while a
# multi-attempt call gets proportionally more room to harvest a worker
# that is about to finish — and a retry is never started while a
# provider worker is still live.
EARNINGS_DRAIN_GRACE_S = 0.15

# Workers that outlive the drain grace stay referenced here until their
# thread finishes — explicit ownership so nothing escapes untracked and
# no 'exception was never retrieved' noise is left behind.
_PENDING_PROVIDER_WORKERS: set[asyncio.Task] = set()

# ROB-1384: module-level wait so tests can replace it with wait_none().
FINNHUB_EARNINGS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


def _track_provider_worker(worker: asyncio.Task) -> None:
    """Own a still-running provider worker until its thread ends."""

    def _reap(done: asyncio.Task) -> None:
        _PENDING_PROVIDER_WORKERS.discard(done)
        if not done.cancelled():
            done.exception()  # consume — never leave 'never retrieved'

    _PENDING_PROVIDER_WORKERS.add(worker)
    worker.add_done_callback(_reap)


async def _drain_provider_worker(worker: asyncio.Task) -> None:
    """Await a provider worker thread to real completion, surviving
    repeated caller cancellation — ``asyncio.to_thread`` threads cannot
    be killed, so returning early would orphan a live SDK call.
    ``asyncio.wait`` never cancels the worker, unlike ``wait_for``."""
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            continue  # repeated cancels keep draining — never abandon
    if worker.done() and not worker.cancelled():
        worker.exception()  # consume — never leave 'never retrieved'


def _earnings_setting(name: str, default: Any) -> Any:
    """Lazy settings read — app config을 import 전제조건으로 만들지 않는다."""
    env_value = os.getenv(name)
    if env_value is not None:
        try:
            return type(default)(env_value)
        except (TypeError, ValueError):
            return default
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001 — config 부재 환경에서도 동작
        return default
    return getattr(settings, name, default)


def _is_retryable_earnings_error(exc: BaseException) -> bool:
    """타임아웃/네트워크/5xx만 재시도. 429·4xx·설정오류는 즉시 실패."""
    if isinstance(exc, FinnhubQuotaExceededError):
        return False
    if isinstance(exc, ProviderWorkerStillRunningError):
        # A live provider worker still holds the client — retrying would
        # overlap a second call on it, so this timeout fails closed.
        return False
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, requests.RequestException):
        return True
    try:
        import finnhub
    except ImportError:  # pragma: no cover - dependency guard
        return False
    if isinstance(exc, finnhub.FinnhubAPIException):
        status_code = getattr(exc, "status_code", None)
        return isinstance(status_code, int) and status_code >= 500
    return False


def _get_finnhub_client() -> Any:
    try:
        import finnhub
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "finnhub-python is required to use Finnhub providers"
        ) from exc

    from app.core.config import settings

    api_key = settings.finnhub_api_key
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    timeout_s: float | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub earningsCalendar rows for ingestion.

    Raises FinnhubQuotaExceededError on HTTP 429; all other SDK exceptions
    propagate unchanged.

    ROB-1384: each attempt is bounded by ``FINNHUB_EARNINGS_TIMEOUT_S``
    (per-attempt, not total) and transient errors are retried up to
    ``FINNHUB_EARNINGS_MAX_ATTEMPTS`` attempts. Quota (429) and
    non-retryable errors still fail immediately.
    """
    per_attempt_timeout = (
        timeout_s
        if timeout_s is not None
        else float(
            _earnings_setting("FINNHUB_EARNINGS_TIMEOUT_S", DEFAULT_EARNINGS_TIMEOUT_S)
        )
    )
    attempts = (
        max_attempts
        if max_attempts is not None
        else int(
            _earnings_setting(
                "FINNHUB_EARNINGS_MAX_ATTEMPTS", DEFAULT_EARNINGS_MAX_ATTEMPTS
            )
        )
    )

    client = _get_finnhub_client()
    # Align the SDK's own request timeout with the per-attempt bound so the
    # worker thread's lifetime is bounded too — ``asyncio.wait_for`` cannot
    # kill a running thread (ROB-1384 B2). ``DEFAULT_TIMEOUT`` is a plain
    # attribute on the finnhub client (default 10s); setting it on this
    # instance does not affect other users of the class.
    if per_attempt_timeout > 0:
        try:
            client.DEFAULT_TIMEOUT = per_attempt_timeout
        except Exception:  # noqa: BLE001 — duck-typed clients may forbid it
            pass

    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    def fetch_sync() -> dict[str, Any]:
        return client.earnings_calendar(
            symbol=symbol.upper() if symbol else "",
            _from=from_date,
            to=to_date,
        )

    # The whole call — including every drain after a timed-out attempt —
    # never outlives its own configured budget: attempts * (timeout +
    # grace). Loop-clock deadlines are immune to thread scheduling
    # overshoot; a still-running worker past the budget is tracked and
    # the call ends non-retryably instead of waiting on provider EOF.
    loop = asyncio.get_running_loop()
    call_deadline = loop.time() + max(1, attempts) * (
        per_attempt_timeout + EARNINGS_DRAIN_GRACE_S
    )

    result: dict[str, Any] | None = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_EARNINGS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_earnings_error),
        reraise=True,
    ):
        with attempt:
            # Shield keeps the worker task itself alive through wait_for's
            # cancellation so it can be drained below — a cancelled task's
            # awaitable ends immediately while the thread keeps running.
            # A non-positive budget means the provider is never invoked —
            # the attempt is already out of time before it starts.
            if per_attempt_timeout <= 0:
                raise TimeoutError(
                    f"non-positive attempt timeout ({per_attempt_timeout}s)"
                )
            worker = asyncio.ensure_future(asyncio.to_thread(fetch_sync))
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=per_attempt_timeout
                )
            except TimeoutError:
                # wait_for cancels only the awaiter — the underlying provider
                # thread keeps running. Drain it via asyncio.wait (which
                # never cancels the worker) bounded by the call's own
                # budget — an unbounded wait would let a slow-drip response
                # (read gaps below the SDK timeout) extend the deadline
                # indefinitely (ROB-1384 B2, R2-F2).
                try:
                    done, _pending = await asyncio.wait(
                        {worker},
                        timeout=max(0.0, call_deadline - loop.time()),
                    )
                except asyncio.CancelledError:
                    await _drain_provider_worker(worker)
                    raise
                if worker not in done:
                    # Still alive past the grace — keep explicit ownership so
                    # the thread is reaped when it ends, and fail this call
                    # non-retryably: a retry would overlap a live worker.
                    _track_provider_worker(worker)
                    raise ProviderWorkerStillRunningError(
                        f"finnhub earnings_calendar worker for {symbol!r} "
                        f"outlived the call budget "
                        f"({max(1, attempts)} attempts x {per_attempt_timeout}s "
                        f"+ {EARNINGS_DRAIN_GRACE_S}s grace)"
                    )
                if worker.cancelled():
                    raise asyncio.CancelledError
                # The worker's real outcome arrived during the drain — a
                # late exception keeps its own classification (a 429 must
                # not be discarded and retried as a bare timeout; R2-F1).
                exc = worker.exception()
                if exc is not None:
                    if getattr(exc, "status_code", None) == 429:
                        raise FinnhubQuotaExceededError(
                            str(exc), status_code=429
                        ) from exc
                    raise exc
                # Worker finished cleanly just past the deadline — discard
                # the late result; this attempt already timed out.
                raise
            except asyncio.CancelledError:
                # Threads cannot be killed — drain the worker to real
                # completion before propagating so cancellation never
                # orphans a live provider call (ROB-1384 R2-F2).
                await _drain_provider_worker(worker)
                raise
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code == 429:
                    raise FinnhubQuotaExceededError(str(exc), status_code=429) from exc
                raise

    if not result or not result.get("earningsCalendar"):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": from_date,
            "to_date": to_date,
            "count": 0,
            "earnings": [],
        }

    earnings = []
    for item in result.get("earningsCalendar", []):
        earnings.append(
            {
                "symbol": item.get("symbol", ""),
                "date": item.get("date"),
                "hour": item.get("hour", ""),
                "eps_estimate": item.get("epsEstimate"),
                "eps_actual": item.get("epsActual"),
                "revenue_estimate": item.get("revenueEstimate"),
                "revenue_actual": item.get("revenueActual"),
                "quarter": item.get("quarter"),
                "year": item.get("year"),
            }
        )

    return {
        "symbol": symbol,
        "instrument_type": "equity_us",
        "source": "finnhub",
        "from_date": from_date,
        "to_date": to_date,
        "count": len(earnings),
        "earnings": earnings,
    }
