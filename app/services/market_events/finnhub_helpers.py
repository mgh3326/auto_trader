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


DEFAULT_EARNINGS_TIMEOUT_S = 8.0
DEFAULT_EARNINGS_MAX_ATTEMPTS = 3

# ROB-1384: module-level wait so tests can replace it with wait_none().
FINNHUB_EARNINGS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


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
    client = _get_finnhub_client()

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

    result: dict[str, Any] | None = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_EARNINGS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_earnings_error),
        reraise=True,
    ):
        with attempt:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(fetch_sync), timeout=per_attempt_timeout
                )
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
