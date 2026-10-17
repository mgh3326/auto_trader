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

Scope decision (ROB-1384 r5): the earnings lookup uses no threads at
all — ``asyncio.to_thread`` workers abandoned after a timeout pile up
on the loop's shared default executor (R4-F1) and can starve unrelated
work. Each attempt issues a plain ``httpx.AsyncClient`` GET instead:
on ``wait_for`` timeout or caller cancellation the request coroutine
is cancelled and the connection is actually closed — nothing is left
behind. Worst-case total time of one call is
``max_attempts * per_attempt_timeout + (max_attempts - 1) * retry-wait
upper bound`` (default ``wait_exponential_jitter(max=2.0)`` → +2s per
gap). Caller cancellation propagates immediately.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from typing import Any

import httpx
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


class FinnhubEarningsHTTPError(Exception):
    """Non-2xx HTTP status from the Finnhub earnings endpoint (r5).

    Carries ``status_code`` so the retryable classifier applies the same
    5xx-vs-4xx rule the SDK path used. The message deliberately contains
    only the status and the API path — never request URLs or header
    values, so no API-key material can leak via exception text.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FinnhubEarningsTransportError(Exception):
    """Sanitized network-layer failure from the earnings endpoint (r5).

    Retryable, like the transport exceptions it replaces — the message
    carries only the httpx exception class name and the API path, never
    the request URL or header values.
    """


DEFAULT_EARNINGS_TIMEOUT_S = 8.0
DEFAULT_EARNINGS_MAX_ATTEMPTS = 3

# ROB-1384 r5: the earnings fetch calls Finnhub over plain HTTP — the
# SDK's base URL and endpoint, with the API key carried in the
# X-Finnhub-Token header (never in the query, where exception text and
# logs would expose it).
_FINNHUB_EARNINGS_URL = "https://api.finnhub.io/api/v1/calendar/earnings"
_FINNHUB_EARNINGS_PATH = "/api/v1/calendar/earnings"

# ROB-1384: module-level wait so tests can replace it with wait_none().
FINNHUB_EARNINGS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


def _new_earnings_http_client(timeout_s: float) -> httpx.AsyncClient:
    """Fresh async client per attempt — module-level seam so tests can
    inject a transport. Redirects are never followed."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)


def _finnhub_api_key() -> str:
    """Return the stripped Finnhub API key, failing before any HTTP request
    when the value could not be sent safely.

    ROB-1384 r5 (R5-F1): a key carrying whitespace/control/non-ASCII
    characters makes h11 raise LocalProtocolError whose message echoes the
    header value — i.e. the key. Validate first so the value never reaches
    the transport layer, and never include it in the error message.
    """
    from app.core.config import settings

    api_key = (settings.finnhub_api_key or "").strip()
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    if (
        not api_key.isascii()
        or not api_key.isprintable()
        or any(ch.isspace() for ch in api_key)
    ):
        raise ValueError(
            "FINNHUB_API_KEY contains characters that are not legal in an HTTP header"
        )
    return api_key


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
    """타임아웃/전송 오류/5xx만 재시도. 429·4xx·설정오류는 즉시 실패 (r5)."""
    if isinstance(exc, FinnhubQuotaExceededError):
        return False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, FinnhubEarningsTransportError):
        return True
    if isinstance(exc, FinnhubEarningsHTTPError):
        return exc.status_code >= 500
    return False


async def fetch_earnings_calendar_finnhub(
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    timeout_s: float | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub earningsCalendar rows for ingestion.

    Raises FinnhubQuotaExceededError on HTTP 429; other HTTP status and
    transport failures surface as sanitized internal errors (r5).

    ROB-1384: each attempt is bounded by ``FINNHUB_EARNINGS_TIMEOUT_S``
    (per-attempt, not total) and transient errors are retried up to
    ``FINNHUB_EARNINGS_MAX_ATTEMPTS`` attempts. Quota (429) and
    non-retryable errors still fail immediately.

    ROB-1384 r5 scope decision: no threads — each attempt is a direct
    ``httpx.AsyncClient`` GET, so a wait_for timeout or caller
    cancellation cancels the request coroutine and closes the
    connection; nothing is left behind. Worst-case total time is
    ``max_attempts * per_attempt_timeout + (max_attempts - 1) * retry
    wait upper bound``. The API key travels only in the
    ``X-Finnhub-Token`` header, never in the query string.
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

    if not from_date:
        from_date = datetime.date.today().isoformat()
    if not to_date:
        to_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    api_key = _finnhub_api_key()  # ValueError if unset — fails once, closed

    result: dict[str, Any] | None = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_EARNINGS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_earnings_error),
        reraise=True,
    ):
        with attempt:
            # A non-positive budget means the provider is never invoked —
            # the attempt is already out of time before it starts.
            if per_attempt_timeout <= 0:
                raise TimeoutError(
                    f"non-positive attempt timeout ({per_attempt_timeout}s)"
                )
            try:
                # ROB-1384 r5: a fresh async client per attempt — no
                # threads. wait_for bounds the whole attempt INCLUDING
                # client close (R5-S1): on timeout or caller cancellation
                # the coroutine is cancelled and the connection closes.
                # The API key goes in the X-Finnhub-Token header only.
                async def _attempt_request() -> httpx.Response:
                    async with _new_earnings_http_client(per_attempt_timeout) as client:
                        return await client.get(
                            _FINNHUB_EARNINGS_URL,
                            params={
                                "from": from_date,
                                "to": to_date,
                                "symbol": symbol.upper() if symbol else "",
                                "international": "false",
                            },
                            headers={"X-Finnhub-Token": api_key},
                        )

                response = await asyncio.wait_for(
                    _attempt_request(), timeout=per_attempt_timeout
                )
            except TimeoutError as exc:
                # Re-raise with a non-empty, diagnosable message — a bare
                # builtin TimeoutError stringifies to "" (ROB-1384 B1).
                raise TimeoutError(
                    f"finnhub earnings_calendar attempt "
                    f"{attempt.retry_state.attempt_number} timed out "
                    f"after {per_attempt_timeout}s for {symbol!r}"
                ) from exc
            except httpx.TimeoutException:
                raise TimeoutError(
                    f"finnhub earnings_calendar attempt "
                    f"{attempt.retry_state.attempt_number} hit the httpx "
                    f"timeout ({per_attempt_timeout}s) for {symbol!r}"
                ) from None
            except httpx.TransportError as exc:
                raise FinnhubEarningsTransportError(
                    f"finnhub earnings_calendar transport error "
                    f"{type(exc).__name__} for {symbol!r} "
                    f"(path {_FINNHUB_EARNINGS_PATH})"
                ) from None
            except httpx.HTTPError as exc:
                raise FinnhubEarningsHTTPError(
                    f"finnhub earnings_calendar request error "
                    f"{type(exc).__name__} for {symbol!r} "
                    f"(path {_FINNHUB_EARNINGS_PATH})",
                    status_code=0,
                ) from None

            status = response.status_code
            if status == 429:
                raise FinnhubQuotaExceededError(
                    f"finnhub earnings_calendar quota exceeded (HTTP 429) "
                    f"for {symbol!r}",
                    status_code=429,
                )
            if status >= 300:
                raise FinnhubEarningsHTTPError(
                    f"finnhub earnings_calendar HTTP {status} for "
                    f"{symbol!r} (path {_FINNHUB_EARNINGS_PATH})",
                    status_code=status,
                )
            # ROB-1384 r6 (R5-F2): a 2xx only counts as success when it is
            # really an earnings calendar payload — same bar the SDK
            # applies. Provider error pages (e.g. text/html) and
            # ``{"error": …}`` bodies must fail closed, never read as
            # "no earnings". Messages carry path+status only — no body,
            # no key material.
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                raise ValueError(
                    f"finnhub earnings_calendar non-JSON content-type "
                    f"(HTTP {status}) for {symbol!r} "
                    f"(path {_FINNHUB_EARNINGS_PATH})"
                )
            try:
                payload = response.json()
            except Exception:
                raise ValueError(
                    f"finnhub earnings_calendar returned non-JSON body "
                    f"for {symbol!r} (path {_FINNHUB_EARNINGS_PATH})"
                ) from None
            if not isinstance(payload, dict):
                raise ValueError(
                    f"finnhub earnings_calendar returned non-object JSON "
                    f"for {symbol!r} (path {_FINNHUB_EARNINGS_PATH})"
                )
            if "error" in payload:
                raise ValueError(
                    f"finnhub earnings_calendar returned an error object "
                    f"(HTTP {status}) for {symbol!r} "
                    f"(path {_FINNHUB_EARNINGS_PATH})"
                )
            if not isinstance(payload.get("earningsCalendar"), list):
                raise ValueError(
                    f"finnhub earnings_calendar missing earningsCalendar "
                    f"list (HTTP {status}) for {symbol!r} "
                    f"(path {_FINNHUB_EARNINGS_PATH})"
                )
            result = payload

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
