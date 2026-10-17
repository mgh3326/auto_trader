"""Finnhub news provider helpers for service-layer consumers.

This module intentionally avoids importing ``app.mcp_server`` so API/research
services can fetch Finnhub headlines without triggering MCP tool registration or
broker/order settings at import time.

ROB-1388 — the company/general news fetch previously ran the synchronous SDK
inside ``asyncio.to_thread`` under ``asyncio.wait_for``: a timed-out attempt
cannot kill the worker thread, so slow-drip responses piled up on the loop's
shared default executor and starved unrelated ``to_thread`` work (ROB-1384
R4-F1 measured the 20-worker pool exhausted after 7 failures). Each attempt
now issues a plain ``httpx.AsyncClient`` GET instead — on ``wait_for`` timeout
or caller cancellation the request coroutine is cancelled and the connection
is actually closed; nothing is left behind. Worst-case total time of one call
is ``max_attempts * per_attempt_timeout + (max_attempts - 1) * retry-wait
upper bound`` (default ``wait_exponential_jitter(max=2.0)`` → +2s per gap).
Caller cancellation propagates immediately.

R6-F3 accepted risk (carried from ROB-1384): a client close that stalls AFTER
the request already timed out or was cancelled can still hold the caller past
that budget — httpcore closes TLS without waiting for the peer and no
FD/socket leak was observed. All exception conversions raise outside the
except block so the raw httpx/h11 exception never survives in ``__context__``
(ROB-1384 R6-F1).

The synchronous SDK client (``_get_finnhub_client``) remains only for
consumers outside this PR's scope — the profile/financials path in
``app.mcp_server.tooling.fundamentals_sources_finnhub`` and the market
valuation fallback — which keep the same abandoned-thread exposure until
they are converted.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.services.market_events.finnhub_helpers import _finnhub_api_key

logger = logging.getLogger(__name__)

try:
    import finnhub
except ImportError:  # pragma: no cover - dependency presence varies by env
    finnhub = None


class FinnhubNewsHTTPError(Exception):
    """Non-2xx HTTP status from the Finnhub news endpoints (ROB-1388).

    Carries ``status_code`` so the retryable classifier applies the same
    5xx-vs-4xx rule the SDK path used, plus the ROB-510 policy that 429 is
    transient. The message deliberately contains only the status and the
    API path — never request URLs or header values, so no API-key material
    can leak via exception text.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FinnhubNewsTransportError(Exception):
    """Sanitized network-layer failure from the Finnhub news endpoints.

    Retryable, like the transport exceptions it replaces — the message
    carries only the httpx exception class name and the API path, never
    the request URL or header values.
    """


# ROB-1388: the news fetch calls Finnhub over plain HTTP — the SDK's base
# URL and endpoints, with the API key carried in the X-Finnhub-Token
# header (never in the query, where exception text and logs would expose
# it).
_FINNHUB_NEWS_COMPANY_URL = "https://api.finnhub.io/api/v1/company-news"
_FINNHUB_NEWS_COMPANY_PATH = "/api/v1/company-news"
_FINNHUB_NEWS_GENERAL_URL = "https://api.finnhub.io/api/v1/news"
_FINNHUB_NEWS_GENERAL_PATH = "/api/v1/news"

DEFAULT_NEWS_TIMEOUT_S = 8.0
DEFAULT_NEWS_MAX_ATTEMPTS = 3

# ROB-510: 테스트에서 monkeypatch로 대기 제거 가능하도록 모듈 레벨 상수
FINNHUB_NEWS_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=2.0)


def _new_news_http_client(timeout_s: float) -> httpx.AsyncClient:
    """Fresh async client per attempt — module-level seam so tests can
    inject a transport. Redirects are never followed."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)


def _news_setting(name: str, default: Any) -> Any:
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


def _is_retryable_news_error(exc: BaseException) -> bool:
    """타임아웃/전송 오류/5xx/429만 재시도. 그 밖의 4xx·설정오류·계약오류는 즉시 실패."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, FinnhubNewsTransportError):
        return True
    if isinstance(exc, FinnhubNewsHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _get_finnhub_api_key() -> str | None:
    """Return Finnhub API key without making app config an import prerequisite."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if api_key:
        return api_key

    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unable to load app settings for Finnhub key: %s", exc)
        return None

    return getattr(settings, "finnhub_api_key", None)


def _get_finnhub_client() -> Any:
    if finnhub is None:
        raise ImportError("finnhub-python is required to use Finnhub providers")
    api_key = _get_finnhub_api_key()
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set")
    return finnhub.Client(api_key=api_key)


async def fetch_news_finnhub(
    symbol: str,
    market: str,
    limit: int,
    *,
    timeout_s: float | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch and normalize Finnhub news using the existing MCP response shape.

    ROB-510: per-attempt timeout + bounded exponential-backoff retry.
    Timeout, transport and 5xx/429 failures are retried; other 4xx and
    malformed 2xx payloads fail immediately.

    ROB-1388: no threads — each attempt is a direct ``httpx.AsyncClient``
    GET (company-news or general news, the same endpoints the SDK used),
    so a wait_for timeout or caller cancellation cancels the request
    coroutine and closes the connection; nothing is left behind on the
    default executor. Client enter+request+close run inside the wait_for
    budget; a close that stalls after the request already timed out or
    was cancelled can still push the return past that budget — the
    accepted R6-F3 risk carried from ROB-1384. The API key travels only
    in the ``X-Finnhub-Token`` header, never in the query string.
    """
    per_attempt_timeout = (
        timeout_s
        if timeout_s is not None
        else float(_news_setting("FINNHUB_NEWS_TIMEOUT_S", DEFAULT_NEWS_TIMEOUT_S))
    )
    attempts = (
        max_attempts
        if max_attempts is not None
        else int(_news_setting("FINNHUB_NEWS_MAX_ATTEMPTS", DEFAULT_NEWS_MAX_ATTEMPTS))
    )
    to_date = datetime.date.today()
    from_date = to_date - datetime.timedelta(days=7)

    api_key = _finnhub_api_key()  # ValueError if unset/invalid — fails once, closed

    if market == "crypto":
        url = _FINNHUB_NEWS_GENERAL_URL
        path = _FINNHUB_NEWS_GENERAL_PATH
        # SDK general_news("crypto", min_id=0) — same category/minId query.
        params: dict[str, Any] = {"category": "crypto", "minId": 0}
    else:
        url = _FINNHUB_NEWS_COMPANY_URL
        path = _FINNHUB_NEWS_COMPANY_PATH
        params = {
            "symbol": symbol.upper(),
            "from": from_date.strftime("%Y-%m-%d"),
            "to": to_date.strftime("%Y-%m-%d"),
        }

    news_items: list[dict[str, Any]] = []
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=FINNHUB_NEWS_RETRY_WAIT,
        retry=retry_if_exception(_is_retryable_news_error),
        reraise=True,
    ):
        with attempt:
            # A non-positive budget means the provider is never invoked —
            # the attempt is already out of time before it starts.
            if per_attempt_timeout <= 0:
                raise TimeoutError(
                    f"non-positive attempt timeout ({per_attempt_timeout}s)"
                )
            # ROB-1384 r7 (R6-F1): converted exceptions are RAISED outside
            # the except block so neither __cause__ nor __context__ keeps
            # the raw httpx/h11 exception (which can echo key material,
            # e.g. a peer-reflected header value in a bad status line).
            # The except block only captures the sanitized exception.
            failure: BaseException | None = None
            response: httpx.Response | None = None
            try:
                # ROB-1388: a fresh async client per attempt — no threads.
                # wait_for bounds enter+request+client close as one
                # coroutine — a close stalled AFTER the request already
                # timed out/cancelled can still run past the budget; that
                # residual is the accepted R6-F3 risk (httpcore closes TLS
                # without waiting for the peer). The API key goes in the
                # X-Finnhub-Token header only.
                async def _attempt_request() -> httpx.Response:
                    async with _new_news_http_client(per_attempt_timeout) as client:
                        return await client.get(
                            url,
                            params=params,
                            headers={"X-Finnhub-Token": api_key},
                        )

                response = await asyncio.wait_for(
                    _attempt_request(), timeout=per_attempt_timeout
                )
            except TimeoutError:
                # Raise a fresh, non-empty TimeoutError AFTER the except
                # block — a bare builtin one stringifies to "" (ROB-1384
                # B1) and the original (plus any CancelledError chain
                # behind it) must not survive in the chain (R6-F1).
                failure = TimeoutError(
                    f"finnhub news attempt {attempt.retry_state.attempt_number} "
                    f"timed out after {per_attempt_timeout}s for {symbol!r} "
                    f"market={market!r}"
                )
            except httpx.TimeoutException:
                failure = TimeoutError(
                    f"finnhub news attempt {attempt.retry_state.attempt_number} "
                    f"hit the httpx timeout ({per_attempt_timeout}s) for "
                    f"{symbol!r} market={market!r}"
                )
            except httpx.TransportError as exc:
                failure = FinnhubNewsTransportError(
                    f"finnhub news transport error {type(exc).__name__} for "
                    f"{symbol!r} market={market!r} (path {path})"
                )
            except httpx.HTTPError as exc:
                failure = FinnhubNewsHTTPError(
                    f"finnhub news request error {type(exc).__name__} for "
                    f"{symbol!r} market={market!r} (path {path})",
                    status_code=0,
                )
            if failure is not None:
                raise failure
            assert response is not None  # failure would have raised above

            status = response.status_code
            if status >= 300:
                raise FinnhubNewsHTTPError(
                    f"finnhub news HTTP {status} for {symbol!r} "
                    f"market={market!r} (path {path})",
                    status_code=status,
                )
            # ROB-1388: a 2xx only counts as success when it is really a
            # news list — provider error pages (e.g. text/html) and
            # ``{"error": …}`` bodies must fail closed, never read as
            # "no news". Messages carry path+status only — no body, no
            # key material.
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                raise ValueError(
                    f"finnhub news non-JSON content-type (HTTP {status}) "
                    f"for {symbol!r} market={market!r} (path {path})"
                )
            payload: Any = None
            json_failed = False
            try:
                payload = response.json()
            except Exception:
                json_failed = True
            if json_failed:
                raise ValueError(
                    f"finnhub news returned non-JSON body for {symbol!r} "
                    f"market={market!r} (path {path})"
                )
            if not isinstance(payload, list):
                raise ValueError(
                    f"finnhub news returned non-list JSON for {symbol!r} "
                    f"market={market!r} (path {path})"
                )
            # Every row must be a dict — a single malformed row fails the
            # whole response (never partial success, which downstream
            # would read as "no relevant news").
            for row_index, row in enumerate(payload):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"finnhub news row {row_index} is not an object "
                        f"(HTTP {status}) for {symbol!r} market={market!r} "
                        f"(path {path})"
                    )
            news_items = payload[:limit]

    result_items = []
    for item in news_items:
        result_items.append(
            {
                "title": item.get("headline", ""),
                "source": item.get("source", ""),
                "datetime": datetime.datetime.fromtimestamp(
                    item.get("datetime", 0)
                ).isoformat()
                if item.get("datetime")
                else None,
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "sentiment": item.get("sentiment"),
                "related": item.get("related", ""),
            }
        )

    return {
        "symbol": symbol,
        "market": market,
        "source": "finnhub",
        "count": len(result_items),
        "news": result_items,
    }


__all__ = [
    "FinnhubNewsHTTPError",
    "FinnhubNewsTransportError",
    "fetch_news_finnhub",
]
