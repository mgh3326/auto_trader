"""Tests for finnhub_helpers 429 mapping (ROB-264) and ROB-1384 retry/timeout.

ROB-1384 r5 — the earnings path is plain async HTTP (httpx.AsyncClient,
no threads). These tests exercise it at the transport seam
(``_new_earnings_http_client``) so timeouts/cancellation hit real
httpx cancellation semantics.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from tenacity import wait_none

from app.services.market_events import finnhub_helpers

# The autouse fixture below replaces this seam — keep a reference to the
# real reader so key-validation tests can exercise the actual code path.
_REAL_KEY_READER = finnhub_helpers._finnhub_api_key

_TEST_API_KEY = "TEST-FINNHUB-KEY-0d1e2f3a4b5c"  # noqa: S105 — sentinel, not a secret

_EARNINGS_ROW = {
    "earningsCalendar": [
        {
            "symbol": "AAPL",
            "date": "2026-05-14",
            "hour": "amc",
            "epsEstimate": 1.2,
            "epsActual": None,
            "revenueEstimate": None,
            "revenueActual": None,
            "quarter": 2,
            "year": 2026,
        }
    ]
}


@pytest.fixture(autouse=True)
def _fast_retry_and_key(monkeypatch: pytest.MonkeyPatch):
    """백오프 제거 + API 키 seam 고정 (ROB-510 패턴과 동일)."""
    monkeypatch.setattr(finnhub_helpers, "FINNHUB_EARNINGS_RETRY_WAIT", wait_none())
    monkeypatch.setattr(finnhub_helpers, "_finnhub_api_key", lambda: _TEST_API_KEY)


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Async transport running a test handler — mock HTTP without threads."""

    def __init__(self, handler) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


class _DripStream(httpx.AsyncByteStream):
    """Emits body bytes a little at a time — a slow-drip response that a
    per-phase read timeout never trips but a wall-clock budget does."""

    def __init__(self, body: bytes, *, chunk_size: int, gap_s: float) -> None:
        self._chunks = [
            body[i : i + chunk_size] for i in range(0, len(body), chunk_size)
        ]
        self._gap_s = gap_s
        self._closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
            await asyncio.sleep(self._gap_s)
        while not self._closed:  # stall: keep dripping like R4-F1
            yield b" "
            await asyncio.sleep(self._gap_s)

    async def aclose(self) -> None:
        self._closed = True


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_ScriptedTransport(handler),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_helpers, "_new_earnings_http_client", factory)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_earnings_calendar_finnhub_maps_429_to_quota_error(
    monkeypatch,
):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            text="API limit reached. Please try again later. Remaining Limit: 0",
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            None, "2026-05-11", "2026-07-17"
        )

    assert exc_info.value.status_code == 429
    assert "429" in str(exc_info.value)
    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_retries_transient_then_succeeds(monkeypatch):
    """ROB-1384 r5 (a) — first attempt exceeds the wall-clock timeout on a
    delayed response; the retry succeeds. Exactly two HTTP calls."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)  # wait_for(0.05) cancels this
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, handler)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-07-17", timeout_s=0.05, max_attempts=2
    )

    assert calls["n"] == 2
    assert result["count"] == 1
    assert result["earnings"][0]["eps_estimate"] == 1.2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_exhausted_retries_reraise(monkeypatch):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_helpers.FinnhubEarningsTransportError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert calls["n"] == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_quota_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="limit")

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_helpers.FinnhubQuotaExceededError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert calls["n"] == 1  # 쿼터는 절대 재시도 안 함


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_fetch_non_retryable_error_fails_once(monkeypatch):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"this is not json",
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(ValueError, match="non-JSON"):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("bad_body", ["[1, 2, 3]", '"just a string"'])
async def test_earnings_fetch_non_dict_json_fails_once(monkeypatch, bad_body):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=bad_body.encode(),
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(ValueError, match="non-object JSON"):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "status,expected_calls", [(400, 1), (404, 1), (500, 3), (503, 3)]
)
async def test_earnings_4xx_fails_once_5xx_uses_all_attempts(
    monkeypatch, status, expected_calls
):
    """ROB-1384 r5 (d) — 4xx errors are not retried; 5xx errors consume
    every attempt before reraising."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="upstream")

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_helpers.FinnhubEarningsHTTPError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )
    assert calls["n"] == expected_calls
    assert exc_info.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_slow_drip_cannot_starve_executor(monkeypatch):
    """ROB-1384 r5 (b) — R4-F1 regression: repeated failures on a
    slow-drip response leave NO threads behind, so the shared executor
    stays free for unrelated work."""
    calls = {"n": 0}
    body = b'{"earningsCalendar": []}' + b" " * 512

    async def drip(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_DripStream(body, chunk_size=4, gap_s=0.02),
        )

    _install_transport(monkeypatch, drip)

    baseline_threads = threading.active_count()
    for _ in range(8):
        with pytest.raises(TimeoutError):
            await finnhub_helpers.fetch_earnings_calendar_finnhub(
                "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.05, max_attempts=1
            )
    assert calls["n"] == 8
    assert threading.active_count() <= baseline_threads

    # The shared default executor is untouched: a normal lookup and an
    # unrelated to_thread job both complete promptly.
    async def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, ok)
    result = await asyncio.wait_for(
        finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=1.0, max_attempts=1
        ),
        timeout=2.0,
    )
    assert result["count"] == 1
    assert await asyncio.wait_for(asyncio.to_thread(lambda: 40 + 2), timeout=2.0) == 42


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_caller_cancellation_returns_immediately(monkeypatch):
    """ROB-1384 r5 (e) — caller cancellation propagates immediately and
    leaves no tasks or threads behind."""
    calls = {"n": 0}

    async def never(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(60)
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, never)

    baseline_threads = threading.active_count()
    baseline_tasks = len(asyncio.all_tasks())

    task = asyncio.ensure_future(
        finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=30.0, max_attempts=1
        )
    )
    await asyncio.sleep(0.02)  # let the request coroutine actually start
    begin = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = asyncio.get_running_loop().time() - begin

    assert elapsed < 1.0  # far below the 30s configured timeout
    assert calls["n"] == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= baseline_tasks
    assert threading.active_count() <= baseline_threads


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_all_timeouts_call_exactly_max_attempts_with_message(
    monkeypatch,
):
    """ROB-1384 r5 (f) — every attempt timing out calls the endpoint
    exactly max_attempts times, then reraises a message-bearing
    TimeoutError."""
    calls = {"n": 0}

    async def slow(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(60)
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, slow)

    with pytest.raises(TimeoutError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.02, max_attempts=3
        )

    assert calls["n"] == 3
    assert str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("bad_timeout", [0.0, -1.0])
async def test_earnings_non_positive_timeout_never_calls_provider(
    monkeypatch, bad_timeout
):
    """ROB-1384 r5 (g) — a degenerate timeout fails before any HTTP
    request is dispatched."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, handler)

    with pytest.raises(TimeoutError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL",
            "2026-05-11",
            "2026-05-11",
            timeout_s=bad_timeout,
            max_attempts=1,
        )

    assert calls["n"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_api_key_travels_in_header_only(monkeypatch):
    """ROB-1384 r5 (h) — the key is in the X-Finnhub-Token header only,
    never in the URL/query, and never in failure exception text."""
    seen: list[httpx.Request] = []

    async def boom(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, boom)

    with pytest.raises(finnhub_helpers.FinnhubEarningsTransportError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=1
        )

    assert seen, "request must have been dispatched"
    request = seen[0]
    assert request.headers.get("x-finnhub-token") == _TEST_API_KEY
    assert _TEST_API_KEY not in str(request.url)
    assert "token" not in request.url.params
    assert _TEST_API_KEY not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert cause is None or _TEST_API_KEY not in repr(cause)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_request_params_match_sdk_semantics(monkeypatch):
    """ROB-1384 r5 (i) — query params mirror finnhub.Client.earnings_calendar:
    from/to dates, upper-cased symbol (or ""), international=false."""
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, handler)

    await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "aapl", "2026-05-11", "2026-05-14"
    )

    assert seen
    params = seen[0].url.params
    assert params["from"] == "2026-05-11"
    assert params["to"] == "2026-05-14"
    assert params["symbol"] == "AAPL"
    assert params["international"] == "false"
    assert seen[0].url.path == "/api/v1/calendar/earnings"

    seen.clear()
    await finnhub_helpers.fetch_earnings_calendar_finnhub(
        None, "2026-05-11", "2026-05-14"
    )
    assert seen[0].url.params["symbol"] == ""


# ── ROB-1384 r6: R5-F1 secret hygiene ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_key",
    [
        "TEST-FINNHUB-KEY-0d1e2f3a4b5c\nrest",
        "key with space",
        "k\ty",
        "k\ney",
        "키-nonascii",
        "k\x01ey",
    ],
    ids=["embedded-newline", "space", "tab", "interior-newline", "nonascii", "control"],
)
async def test_earnings_header_unsafe_key_fails_before_http_without_leak(
    monkeypatch, bad_key
):
    """R5-F1 — a key h11 would echo inside LocalProtocolError must never
    reach the transport: zero HTTP calls, and no exception chain carries
    the value."""
    import traceback

    import app.core.config

    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, handler)
    # Exercise the REAL key reader against real settings — the autouse
    # fixture replaced it for the other tests.
    monkeypatch.setattr(finnhub_helpers, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(app.core.config.settings, "finnhub_api_key", bad_key)

    with pytest.raises(ValueError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=1
        )

    assert calls["n"] == 0
    assert bad_key not in str(exc_info.value)
    assert bad_key not in repr(exc_info.value)
    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value), exc_info.value, exc_info.value.__traceback__
        )
    )
    assert bad_key not in rendered


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_trailing_whitespace_key_is_stripped(monkeypatch):
    """R5-F1 — surrounding whitespace (the observed trailing-newline case)
    is stripped, so the key sent is a clean header value."""
    import app.core.config

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_EARNINGS_ROW)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(finnhub_helpers, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(
        app.core.config.settings, "finnhub_api_key", _TEST_API_KEY + "\n "
    )

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-05-11"
    )

    assert result["count"] == 1
    assert seen[0].headers.get("x-finnhub-token") == _TEST_API_KEY


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_bad_key_financials_warning_log_has_no_key(monkeypatch, caplog):
    """R5-F1 — the _financials fallback WARNING renders exc_info; the
    sanitized chain must not leak the key there either."""
    import logging

    import app.core.config
    from app.mcp_server.tooling.fundamentals import _financials

    bad_key = "TEST-FINNHUB-KEY with space"
    monkeypatch.setattr(finnhub_helpers, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(app.core.config.settings, "finnhub_api_key", bad_key)

    async def fallback_boom(*_args, **_kwargs):
        raise RuntimeError("fallback store down")

    monkeypatch.setattr(
        _financials, "_fetch_earnings_calendar_market_events_us", fallback_boom
    )

    with caplog.at_level(
        logging.WARNING,
        logger="app.mcp_server.tooling.fundamentals._financials",
    ):
        payload = await _financials.handle_get_earnings_calendar(
            "AAPL", "2026-05-11", "2026-05-11", market="us"
        )

    assert payload["error_code"] == "earnings_lookup_failed"
    assert bad_key not in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_transport_error_chain_is_suppressed(monkeypatch):
    """R5-F1 — converted httpx errors carry no __cause__ chain: the raw
    exception text (request URL etc.) is dropped, only the type name is
    kept in our message."""
    import traceback

    async def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, boom)

    with pytest.raises(finnhub_helpers.FinnhubEarningsTransportError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=1
        )

    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    assert "ConnectError" in str(exc)
    assert _TEST_API_KEY not in str(exc)
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _TEST_API_KEY not in rendered
    # the raw httpx exception text never reaches the rendered chain
    assert "connection refused" not in rendered


# ── ROB-1384 r6: R5-F2 closed 2xx contract ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "response_kwargs,case_id",
    [
        (
            {
                "headers": {"content-type": "text/html"},
                "json": {"error": "upstream temporarily unavailable"},
            },
            "html-error-body",
        ),
        (
            {"json": {"error": "upstream temporarily unavailable"}},
            "json-error-key",
        ),
        ({"json": {"earningsCalendar": {"not": "a list"}}}, "calendar-not-list"),
        ({"json": {"something": "else"}}, "missing-calendar-key"),
    ],
    ids=lambda v: v if isinstance(v, str) else "resp",
)
async def test_earnings_invalid_2xx_body_fails_closed_once(
    monkeypatch, response_kwargs, case_id
):
    """R5-F2 — a 2xx is success only when it is a real earningsCalendar
    payload (SDK parity); anything else is a non-retryable failure whose
    message carries path+status only."""
    del case_id
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, **response_kwargs)

    _install_transport(monkeypatch, handler)

    with pytest.raises(ValueError) as exc_info:
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", max_attempts=3
        )

    assert calls["n"] == 1  # non-retryable
    msg = str(exc_info.value)
    assert "/api/v1/calendar/earnings" in msg
    assert "upstream temporarily unavailable" not in msg
    assert _TEST_API_KEY not in msg


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_empty_calendar_list_is_success(monkeypatch):
    """R5-F2 — the legitimate empty schedule still counts as success."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"earningsCalendar": []})

    _install_transport(monkeypatch, handler)

    result = await finnhub_helpers.fetch_earnings_calendar_finnhub(
        "AAPL", "2026-05-11", "2026-05-11"
    )

    assert calls["n"] == 1
    assert result["count"] == 0
    assert result.get("error_code") is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_html_error_body_fails_closed_through_callers(
    monkeypatch,
):
    """R5-F2 caller level — the provider error page surfaces as a closed
    failure (error_code, freshness never 'live'), not count=0/live."""
    import datetime

    from app.mcp_server.tooling import earnings_context as ec
    from app.mcp_server.tooling.fundamentals import _financials

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            json={"error": "upstream temporarily unavailable"},
        )

    _install_transport(monkeypatch, handler)

    async def empty_fallback(*_args, **_kwargs):
        return {"count": 0}

    monkeypatch.setattr(
        _financials, "_fetch_earnings_calendar_market_events_us", empty_fallback
    )

    tool_result = await _financials.handle_get_earnings_calendar(
        "AAPL", "2026-05-11", "2026-05-11", market="us"
    )
    assert tool_result["error_code"] == "earnings_lookup_failed"
    assert tool_result.get("count", 0) == 0

    ctx = await ec.build_earnings_context(
        "AAPL", "us", today=datetime.date(2026, 5, 11)
    )
    assert ctx is not None
    assert ctx["freshness"] != "live"
    assert ctx["freshness"] == "unknown"
    assert ctx["has_upcoming"] is False


# ── ROB-1384 r6: R5-S1 close inside the attempt budget ───────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_attempt_budget_includes_client_close(monkeypatch):
    """R5-S1 — ``async with`` exit runs inside the wait_for budget: a
    stuck transport aclose cannot hold the caller past the timeout."""
    close_entered = asyncio.Event()

    class _HangingCloseTransport(_ScriptedTransport):
        async def aclose(self) -> None:
            close_entered.set()
            await asyncio.sleep(60)

    async def quick(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EARNINGS_ROW)

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_HangingCloseTransport(quick),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_helpers, "_new_earnings_http_client", factory)

    begin = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=0.05, max_attempts=1
        )
    elapsed = asyncio.get_running_loop().time() - begin

    assert close_entered.is_set()  # the hang was inside __aexit__
    assert elapsed <= 0.05 + 1.0  # budget + small margin, not the 60s hang


@pytest.mark.asyncio
@pytest.mark.unit
async def test_earnings_caller_cancel_during_client_close_propagates(
    monkeypatch,
):
    """R5-S1 — cancellation during a stuck client close still surfaces
    as CancelledError, not a swallowed/delayed timeout."""
    close_entered = asyncio.Event()

    class _HangingCloseTransport(_ScriptedTransport):
        async def aclose(self) -> None:
            close_entered.set()
            await asyncio.sleep(60)

    async def quick(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EARNINGS_ROW)

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_HangingCloseTransport(quick),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_helpers, "_new_earnings_http_client", factory)

    task = asyncio.ensure_future(
        finnhub_helpers.fetch_earnings_calendar_finnhub(
            "AAPL", "2026-05-11", "2026-05-11", timeout_s=30.0, max_attempts=1
        )
    )
    assert await asyncio.wait_for(close_entered.wait(), timeout=2.0)
    begin = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - begin < 1.0


@pytest.mark.unit
def test_earnings_retryable_classifier():
    h = finnhub_helpers
    assert h._is_retryable_earnings_error(TimeoutError()) is True
    assert h._is_retryable_earnings_error(httpx.TimeoutException("t")) is True
    assert h._is_retryable_earnings_error(httpx.ConnectError("boom")) is True
    assert h._is_retryable_earnings_error(h.FinnhubEarningsTransportError("x")) is True
    assert (
        h._is_retryable_earnings_error(h.FinnhubEarningsHTTPError("x", status_code=503))
        is True
    )
    assert (
        h._is_retryable_earnings_error(h.FinnhubEarningsHTTPError("x", status_code=403))
        is False
    )
    assert (
        h._is_retryable_earnings_error(h.FinnhubEarningsHTTPError("x", status_code=429))
        is False
    )
    assert h._is_retryable_earnings_error(h.FinnhubQuotaExceededError("q")) is False
    assert h._is_retryable_earnings_error(ValueError("x")) is False
    assert h._is_retryable_earnings_error(ImportError("x")) is False
