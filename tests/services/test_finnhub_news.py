"""Tests for app.services.finnhub_news (ROB-510 retry contract + ROB-1388).

ROB-1388 — the news fetch is plain async HTTP (httpx.AsyncClient, no
threads). These tests exercise it at the transport seam
(``_new_news_http_client``) and at a real loopback TCP peer, so
timeouts/cancellation hit real httpx cancellation semantics and real
wire bytes.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import threading
import traceback
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from tenacity import wait_none

from app.services import finnhub_news, symbol_news_service

# The autouse fixture below replaces this seam — keep a reference to the
# real reader so key-validation tests can exercise the actual code path.
_REAL_KEY_READER = finnhub_news._finnhub_api_key

_TEST_API_KEY = "TEST-FINNHUB-KEY-0d1e2f3a4b5c"  # noqa: S105 — sentinel, not a secret

# Real production-shaped finnhub company-news item.
_NEWS_ITEM = {
    "category": "company news",
    "datetime": 1747000000,
    "headline": "Apple beats Q3 estimates as services revenue jumps",
    "id": 135792468,
    "image": "https://data.finnhub.io/img/aapl.png",
    "related": "AAPL",
    "source": "Reuters",
    "summary": "Apple Inc. reported third-quarter results above estimates.",
    "url": "https://www.reuters.com/technology/apple-q3-2026",
    "sentiment": {"score": 0.62},
}

_CRYPTO_NEWS_ITEM = {
    "category": "crypto",
    "datetime": 1747000100,
    "headline": "Bitcoin holds above $120k as ETF inflows continue",
    "id": 246813579,
    "image": "",
    "related": "",
    "source": "CoinDesk",
    "summary": "Bitcoin consolidated gains amid steady ETF demand.",
    "url": "https://www.coindesk.com/markets/2026/btc-etf-inflows",
}


@pytest.fixture(autouse=True)
def _fast_retry_and_key(monkeypatch: pytest.MonkeyPatch):
    """백오프 제거 + API 키 seam 고정 (ROB-510 패턴과 동일)."""
    monkeypatch.setattr(finnhub_news, "FINNHUB_NEWS_RETRY_WAIT", wait_none())
    monkeypatch.setattr(finnhub_news, "_finnhub_api_key", lambda: _TEST_API_KEY)


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


def _exception_chain(exc: BaseException):
    """Every object reachable via __cause__/__context__ (R6-F1 walk)."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.extend((e.__cause__, e.__context__))


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_ScriptedTransport(handler),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_news, "_new_news_http_client", factory)


def _stored_us(article_id: int, url: str, title: str):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="Reuters",
        published_at=datetime.datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": "pending",
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
        summary="cached summary",
        fetched_at=datetime.datetime(2026, 6, 10, 8, 55),
    )


def _patch_store(monkeypatch: pytest.MonkeyPatch, *, stored, excluded_count=0):
    """Mock the DB persist/load layer — no real Postgres needed."""
    upsert = AsyncMock()
    load = AsyncMock(return_value=(stored, excluded_count))
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_feed_articles", upsert
    )
    monkeypatch.setattr(symbol_news_service.symbol_news_store, "load_symbol_news", load)
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )
    return upsert, load


# ── success shape / request contract ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_company_success_maps_payload(monkeypatch):
    """US company-news: SDK-shaped list maps into the existing envelope."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub("aapl", "us", 10)

    assert result["symbol"] == "aapl"
    assert result["market"] == "us"
    assert result["source"] == "finnhub"
    assert result["count"] == 1
    item = result["news"][0]
    assert item["title"] == _NEWS_ITEM["headline"]
    assert item["source"] == "Reuters"
    assert (
        item["datetime"]
        == datetime.datetime.fromtimestamp(_NEWS_ITEM["datetime"]).isoformat()
    )
    assert item["url"] == _NEWS_ITEM["url"]
    assert item["summary"] == _NEWS_ITEM["summary"]
    assert item["sentiment"] == {"score": 0.62}
    assert item["related"] == "AAPL"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_crypto_success_maps_payload(monkeypatch):
    """crypto market uses general news and keeps the same envelope."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_CRYPTO_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub("KRW-BTC", "crypto", 10)

    assert result["market"] == "crypto"
    assert result["count"] == 1
    assert result["news"][0]["title"] == _CRYPTO_NEWS_ITEM["headline"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_request_params_match_sdk(monkeypatch):
    """Query params mirror the SDK: company-news symbol+from+to, crypto
    general news category=crypto&minId=0."""
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    await finnhub_news.fetch_news_finnhub("aapl", "us", 10)

    assert seen
    request = seen[0]
    assert request.url.path == "/api/v1/company-news"
    params = request.url.params
    assert params["symbol"] == "AAPL"
    today = datetime.date.today()
    assert params["from"] == (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    assert params["to"] == today.strftime("%Y-%m-%d")

    seen.clear()
    await finnhub_news.fetch_news_finnhub("KRW-BTC", "crypto", 10)
    request = seen[0]
    assert request.url.path == "/api/v1/news"
    assert request.url.params["category"] == "crypto"
    assert request.url.params["minId"] == "0"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_api_key_travels_in_header_only(monkeypatch):
    """The key is in the X-Finnhub-Token header only, never in the
    URL/query, and never in failure exception text."""
    seen: list[httpx.Request] = []

    async def boom(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, boom)

    with pytest.raises(finnhub_news.FinnhubNewsTransportError) as exc_info:
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=1)

    assert seen, "request must have been dispatched"
    request = seen[0]
    assert request.headers.get("x-finnhub-token") == _TEST_API_KEY
    assert _TEST_API_KEY not in str(request.url)
    assert "token" not in request.url.params
    assert _TEST_API_KEY not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_limit_slices_items(monkeypatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NEWS_ITEM, _NEWS_ITEM, _NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 2)

    assert result["count"] == 2
    assert len(result["news"]) == 2


# ── retry classification (ROB-510: timeout/transport/5xx/429 retry) ──


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_fetch_retries_transient_then_succeeds(monkeypatch):
    """First attempt exceeds the wall-clock timeout; the retry succeeds."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)  # wait_for(0.05) cancels this
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub(
        "AAPL", "us", 10, timeout_s=0.05, max_attempts=2
    )

    assert calls["n"] == 2
    assert result["count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_fetch_exhausted_retries_reraise(monkeypatch):
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_news.FinnhubNewsTransportError):
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=3)
    assert calls["n"] == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_429_is_retried_then_succeeds(monkeypatch):
    """ROB-510 policy — unlike earnings, news treats 429 as transient."""
    responses = iter([429, 200])
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        status = next(responses)
        if status == 429:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=3)

    assert calls["n"] == 2
    assert result["count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "status,expected_calls",
    [(400, 1), (401, 1), (403, 1), (404, 1), (429, 3), (500, 3), (503, 3)],
)
async def test_news_4xx_fails_once_5xx_429_use_all_attempts(
    monkeypatch, status, expected_calls
):
    """Non-retryable 4xx fail once; 5xx and 429 consume every attempt."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="upstream")

    _install_transport(monkeypatch, handler)

    with pytest.raises(finnhub_news.FinnhubNewsHTTPError) as exc_info:
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=3)

    assert calls["n"] == expected_calls
    assert exc_info.value.status_code == status


@pytest.mark.unit
def test_news_retryable_classifier():
    h = finnhub_news
    assert h._is_retryable_news_error(TimeoutError()) is True
    assert h._is_retryable_news_error(httpx.TimeoutException("t")) is True
    assert h._is_retryable_news_error(httpx.ConnectError("boom")) is True
    assert h._is_retryable_news_error(h.FinnhubNewsTransportError("x")) is True
    assert (
        h._is_retryable_news_error(h.FinnhubNewsHTTPError("x", status_code=503)) is True
    )
    assert (
        h._is_retryable_news_error(h.FinnhubNewsHTTPError("x", status_code=429)) is True
    )
    assert (
        h._is_retryable_news_error(h.FinnhubNewsHTTPError("x", status_code=403))
        is False
    )
    assert h._is_retryable_news_error(ValueError("x")) is False
    assert h._is_retryable_news_error(ImportError("x")) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_all_timeouts_call_exactly_max_attempts_with_message(
    monkeypatch,
):
    calls = {"n": 0}

    async def slow(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(60)
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, slow)

    with pytest.raises(TimeoutError) as exc_info:
        await finnhub_news.fetch_news_finnhub(
            "AAPL", "us", 10, timeout_s=0.02, max_attempts=3
        )

    assert calls["n"] == 3
    assert str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("bad_timeout", [0.0, -1.0])
async def test_news_non_positive_timeout_never_calls_provider(monkeypatch, bad_timeout):
    """A degenerate timeout fails before any HTTP request is dispatched."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)

    with pytest.raises(TimeoutError):
        await finnhub_news.fetch_news_finnhub(
            "AAPL", "us", 10, timeout_s=bad_timeout, max_attempts=1
        )

    assert calls["n"] == 0


# ── ROB-1388 AC1/AC2: real loopback slow-drip, no executor starvation ─


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_slow_drip_loopback_no_executor_or_task_growth(monkeypatch):
    """AC1+AC2 — 20+ failed slow-drip calls on EACH lane (company-news and
    crypto general) over a REAL loopback TCP peer leave no threads, tasks,
    or FDs behind; unrelated ``to_thread`` work still completes promptly."""
    stats = {"requests": 0}

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readuntil(b"\r\n\r\n")
            stats["requests"] += 1
            # Headers arrive promptly; the body then drips one chunked
            # byte at a time — inside the httpx read timeout each chunk,
            # so only the wait_for wall-clock can fire.
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: application/json\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            while True:
                writer.write(b"1\r\n \r\n")
                await writer.drain()
                await asyncio.sleep(0.02)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        finnhub_news,
        "_FINNHUB_NEWS_COMPANY_URL",
        f"http://127.0.0.1:{port}/api/v1/company-news",
    )
    monkeypatch.setattr(
        finnhub_news,
        "_FINNHUB_NEWS_GENERAL_URL",
        f"http://127.0.0.1:{port}/api/v1/news",
    )

    def _fd_count() -> int | None:
        try:
            return len(os.listdir("/dev/fd"))
        except OSError:
            return None

    baseline_threads = threading.active_count()
    baseline_tasks = len(asyncio.all_tasks())
    baseline_fds = _fd_count()

    try:
        for _ in range(21):
            with pytest.raises(TimeoutError):
                await finnhub_news.fetch_news_finnhub(
                    "AAPL", "us", 5, timeout_s=0.2, max_attempts=1
                )
        for _ in range(21):
            with pytest.raises(TimeoutError):
                await finnhub_news.fetch_news_finnhub(
                    "KRW-BTC", "crypto", 5, timeout_s=0.2, max_attempts=1
                )
    finally:
        server.close()
        await server.wait_closed()

    assert stats["requests"] == 42  # every failure really hit the peer

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while len(asyncio.all_tasks()) > baseline_tasks and loop.time() < deadline:
        await asyncio.sleep(0.02)
    if baseline_fds is not None:
        deadline = loop.time() + 2.0
        while _fd_count() > baseline_fds and loop.time() < deadline:
            await asyncio.sleep(0.02)

    assert threading.active_count() <= baseline_threads
    assert len(asyncio.all_tasks()) <= baseline_tasks
    if baseline_fds is not None:
        assert _fd_count() <= baseline_fds + 4  # TIME_WAIT may linger briefly

    # AC2 — the shared default executor was never touched: unrelated
    # to_thread work still completes well inside a normal budget.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: 40 + 2), timeout=2.0) == 42

    # And a healthy provider still answers normally afterwards.
    async def ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, ok)
    result = await asyncio.wait_for(
        finnhub_news.fetch_news_finnhub("AAPL", "us", 5, timeout_s=1.0),
        timeout=2.0,
    )
    assert result["count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_slow_drip_transport_no_thread_growth(monkeypatch):
    """R4-F1 regression at the transport seam — repeated failures on a
    slow-drip response leave NO threads behind, so the shared executor
    stays free for unrelated work."""
    calls = {"n": 0}
    body = b"[]" + b" " * 512

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
            await finnhub_news.fetch_news_finnhub(
                "AAPL", "us", 5, timeout_s=0.05, max_attempts=1
            )
    assert calls["n"] == 8
    assert threading.active_count() <= baseline_threads

    # The shared default executor is untouched: an unrelated to_thread
    # job completes promptly.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: 40 + 2), timeout=2.0) == 42


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_caller_cancellation_returns_immediately(monkeypatch):
    """Caller cancellation propagates immediately and leaves nothing."""
    calls = {"n": 0}

    async def never(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(60)
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, never)

    baseline_threads = threading.active_count()
    baseline_tasks = len(asyncio.all_tasks())

    task = asyncio.ensure_future(
        finnhub_news.fetch_news_finnhub(
            "AAPL", "us", 10, timeout_s=30.0, max_attempts=1
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


# ── R5-S1: close inside the attempt budget ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_attempt_budget_includes_client_close(monkeypatch):
    """``async with`` exit runs inside the wait_for budget: a stuck
    transport aclose cannot hold the caller past the timeout."""
    close_entered = asyncio.Event()

    class _HangingCloseTransport(_ScriptedTransport):
        async def aclose(self) -> None:
            close_entered.set()
            await asyncio.sleep(60)

    async def quick(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NEWS_ITEM])

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_HangingCloseTransport(quick),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_news, "_new_news_http_client", factory)

    begin = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await finnhub_news.fetch_news_finnhub(
            "AAPL", "us", 10, timeout_s=0.05, max_attempts=1
        )
    elapsed = asyncio.get_running_loop().time() - begin

    assert close_entered.is_set()  # the hang was inside __aexit__
    assert elapsed <= 0.05 + 1.0  # budget + small margin, not the 60s hang


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_caller_cancel_during_client_close_propagates(monkeypatch):
    """Cancellation during a stuck client close still surfaces as
    CancelledError, not a swallowed/delayed timeout."""
    close_entered = asyncio.Event()

    class _HangingCloseTransport(_ScriptedTransport):
        async def aclose(self) -> None:
            close_entered.set()
            await asyncio.sleep(60)

    async def quick(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NEWS_ITEM])

    def factory(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_HangingCloseTransport(quick),
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
        )

    monkeypatch.setattr(finnhub_news, "_new_news_http_client", factory)

    task = asyncio.ensure_future(
        finnhub_news.fetch_news_finnhub(
            "AAPL", "us", 10, timeout_s=30.0, max_attempts=1
        )
    )
    assert await asyncio.wait_for(close_entered.wait(), timeout=2.0)
    begin = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - begin < 1.0


# ── R5-F1/R6-F1: key hygiene + empty raw exception chain ─────────────


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
    ids=[
        "embedded-newline",
        "space",
        "tab",
        "interior-newline",
        "nonascii",
        "control",
    ],
)
async def test_news_header_unsafe_key_fails_before_http_without_leak(
    monkeypatch, bad_key
):
    """AC3 — a key h11 would echo inside LocalProtocolError must never
    reach the transport: zero HTTP calls, and no exception chain carries
    the value."""
    import app.core.config

    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)
    # Exercise the REAL key reader against real settings — the autouse
    # fixture replaced it for the other tests.
    monkeypatch.setattr(finnhub_news, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(app.core.config.settings, "finnhub_api_key", bad_key)

    with pytest.raises(ValueError) as exc_info:
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=1)

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
async def test_news_trailing_whitespace_key_is_stripped(monkeypatch):
    """Surrounding whitespace (the observed trailing-newline case) is
    stripped, so the key sent is a clean header value."""
    import app.core.config

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_NEWS_ITEM])

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(finnhub_news, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(
        app.core.config.settings, "finnhub_api_key", _TEST_API_KEY + "\n "
    )

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 10)

    assert result["count"] == 1
    assert seen[0].headers.get("x-finnhub-token") == _TEST_API_KEY


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_bad_key_service_warning_log_has_no_key(monkeypatch, caplog):
    """AC3 — the real caller WARNING site renders the exception; the
    sanitized failure must not leak the key into the log."""
    import app.core.config

    bad_key = "TEST-FINNHUB-KEY with space"
    monkeypatch.setattr(finnhub_news, "_finnhub_api_key", _REAL_KEY_READER)
    monkeypatch.setattr(app.core.config.settings, "finnhub_api_key", bad_key)
    _patch_store(monkeypatch, stored=[])

    with caplog.at_level(logging.WARNING, logger="app.services.symbol_news_service"):
        result = await symbol_news_service.fetch_symbol_news("AAPL", "us")

    assert result.status == "error"
    assert result.error_code == "ValueError"
    assert bad_key not in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_transport_error_chain_is_suppressed(monkeypatch):
    """Converted httpx errors carry no __cause__ chain: the raw exception
    text (request URL etc.) is dropped, only the type name is kept."""

    async def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, boom)

    with pytest.raises(finnhub_news.FinnhubNewsTransportError) as exc_info:
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=1)

    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert "ConnectError" in str(exc)
    assert _TEST_API_KEY not in str(exc)
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _TEST_API_KEY not in rendered
    # the raw httpx exception text never reaches the rendered chain
    assert "connection refused" not in rendered
    for member in _exception_chain(exc):
        assert _TEST_API_KEY not in str(member)
        assert _TEST_API_KEY not in repr(member)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_peer_reflected_key_never_survives_exception_chain(
    monkeypatch, caplog
):
    """AC3 — a real loopback peer echoes the sent X-Finnhub-Token back
    inside a malformed status line; the raw httpx/h11 exception carries
    the key, so the converted exception must keep NO raw chain at all
    (retries exhausted → the tenacity-reraised final exception)."""
    seen_requests: list[bytes] = []

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
            seen_requests.append(data)
            writer.write(b"HTTP/1.1 " + _TEST_API_KEY.encode() + b" junk\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        finnhub_news,
        "_FINNHUB_NEWS_COMPANY_URL",
        f"http://127.0.0.1:{port}/api/v1/company-news",
    )

    try:
        with pytest.raises(finnhub_news.FinnhubNewsTransportError) as ei:
            await finnhub_news.fetch_news_finnhub(
                "AAPL", "us", 10, timeout_s=2.0, max_attempts=3
            )
    finally:
        server.close()
        await server.wait_closed()

    assert len(seen_requests) == 3  # retried, then gave up
    # the key really went over the wire — the peer echoed it in a bad
    # status line, so the raw httpx exception did contain it
    assert _TEST_API_KEY.encode() in seen_requests[0]
    exc = ei.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for member in _exception_chain(exc):
        assert _TEST_API_KEY not in str(member)
        assert _TEST_API_KEY not in repr(member)
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _TEST_API_KEY not in rendered

    # AC3 — exc_info logging of the same exception renders clean too.
    with caplog.at_level(logging.ERROR, logger="app.services.finnhub_news"):
        logging.getLogger("app.services.finnhub_news").error(
            "probe: fetch failed", exc_info=(type(exc), exc, exc.__traceback__)
        )
    assert _TEST_API_KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_kind",
    [
        pytest.param("timeout", id="timeout"),
        pytest.param("transport", id="transport"),
        pytest.param("generic", id="generic-httperror"),
    ],
)
async def test_news_converted_errors_leave_empty_raw_chain(monkeypatch, failure_kind):
    """R6-F1 — timeout / transport / generic-HTTPError conversions all
    raise the sanitized exception with both __cause__ and __context__
    empty, after retries are exhausted."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        if failure_kind == "timeout":
            await asyncio.sleep(60)
            return httpx.Response(200, json=[_NEWS_ITEM])
        if failure_kind == "transport":
            raise httpx.ConnectError("connection refused")
        raise httpx.DecodingError("bad encoding")

    _install_transport(monkeypatch, handler)

    expected = (
        TimeoutError
        if failure_kind == "timeout"
        else (
            finnhub_news.FinnhubNewsTransportError
            if failure_kind == "transport"
            else finnhub_news.FinnhubNewsHTTPError
        )
    )
    with pytest.raises(expected) as exc_info:
        await finnhub_news.fetch_news_finnhub(
            "AAPL",
            "us",
            10,
            timeout_s=0.02 if failure_kind == "timeout" else 2.0,
            max_attempts=3,
        )

    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    chain = list(_exception_chain(exc))
    assert chain == [exc]  # nothing reachable at all
    for member in chain:
        assert _TEST_API_KEY not in str(member)
        assert _TEST_API_KEY not in repr(member)


# ── R5-F2/AC4: closed 2xx contract ───────────────────────────────────


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
            "json-error-dict",
        ),
        ({"json": {"some": "object"}}, "json-dict-not-list"),
        (
            {
                "content": b'"just a string"',
                "headers": {"content-type": "application/json"},
            },
            "json-string",
        ),
        (
            {"content": b"42", "headers": {"content-type": "application/json"}},
            "json-number",
        ),
        ({"json": [{"headline": "ok"}, "bad row"]}, "non-dict-row"),
        (
            {
                "content": b"this is not json",
                "headers": {"content-type": "application/json"},
            },
            "non-json-body",
        ),
        ({"content": b"[]"}, "missing-content-type"),
    ],
    ids=lambda v: v if isinstance(v, str) else "resp",
)
async def test_news_invalid_2xx_body_fails_closed_once(
    monkeypatch, response_kwargs, case_id
):
    """AC4 — a 2xx is success only when it is a real news list; anything
    else is a non-retryable failure whose message carries path+status
    only."""
    del case_id
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, **response_kwargs)

    _install_transport(monkeypatch, handler)

    with pytest.raises(ValueError) as exc_info:
        await finnhub_news.fetch_news_finnhub("AAPL", "us", 10, max_attempts=3)

    assert calls["n"] == 1  # non-retryable
    msg = str(exc_info.value)
    assert "/api/v1/" in msg
    assert "upstream temporarily unavailable" not in msg
    assert "bad row" not in msg
    assert _TEST_API_KEY not in msg


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_empty_list_is_success(monkeypatch):
    """AC4 — a legitimate empty list still counts as success (0 news)."""
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    _install_transport(monkeypatch, handler)

    result = await finnhub_news.fetch_news_finnhub("AAPL", "us", 10)

    assert calls["n"] == 1
    assert result["count"] == 0
    assert result["news"] == []
    assert result["source"] == "finnhub"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_empty_list_via_service_is_live_success(monkeypatch):
    """AC4 positive — a real ``[]`` through the caller is live success,
    not degraded/fallback."""
    from app.mcp_server.tooling.fundamentals import _news as news_tool

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _install_transport(monkeypatch, handler)
    _patch_store(monkeypatch, stored=[])

    payload = await news_tool.handle_get_news("AAPL", "us")

    assert payload["count"] == 0
    assert payload.get("degraded") is None
    assert payload["data_state"] == "fresh"
    assert payload["provider_provenance"][0]["mode"] == "live"
    assert "error_code" not in payload


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("market", ["us", "crypto"])
async def test_news_html_error_body_fails_closed_through_callers(monkeypatch, market):
    """AC4 — a 200 text/html ``{"error": …}`` page is a closed lookup
    failure: the service serves DB-stale rows as degraded+fallback, never
    live."""
    symbol = "KRW-BTC" if market == "crypto" else "AAPL"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            json={"error": "upstream temporarily unavailable"},
        )

    _install_transport(monkeypatch, handler)
    _patch_store(
        monkeypatch,
        stored=[_stored_us(1, "https://r/cached", "Cached headline")],
    )

    result = await symbol_news_service.fetch_symbol_news(symbol, market, limit=10)

    assert result.degraded is True
    assert result.fetch_error == "ValueError"
    assert result.fallback_source == "news_articles"
    assert result.provider_provenance[0]["mode"] == "fallback"
    assert result.provider_provenance[0]["status"] == "error"
    assert result.articles[0].title == "Cached headline"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_html_error_envelope_is_stale_fallback_never_live(
    monkeypatch,
):
    """AC4 envelope level — the get_news payload shows stale+fallback,
    never live/fresh, for a malformed-2xx failure."""
    from app.mcp_server.tooling.fundamentals import _news as news_tool

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            json={"error": "upstream temporarily unavailable"},
        )

    _install_transport(monkeypatch, handler)
    _patch_store(
        monkeypatch,
        stored=[_stored_us(1, "https://r/cached", "Cached headline")],
    )

    payload = await news_tool.handle_get_news("AAPL", "us")

    assert payload.get("error_code") is None  # articles exist — not an error
    assert payload["degraded"] is True
    assert payload["fetch_error"] == "ValueError"
    assert payload["fallback_source"] == "news_articles"
    assert payload["data_state"] == "stale"
    assert payload["stale"] is True
    assert payload["count"] == 1  # cached row served, not fabricated
    modes = [p["mode"] for p in payload["provider_provenance"]]
    assert "live" not in modes
    assert modes == ["fallback"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_html_error_empty_cache_is_news_lookup_failed(monkeypatch):
    """AC4 — with no cached rows the closed failure surfaces as
    news_lookup_failed, never live."""
    from app.mcp_server.tooling.fundamentals import _news as news_tool

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            json={"error": "upstream temporarily unavailable"},
        )

    _install_transport(monkeypatch, handler)
    _patch_store(monkeypatch, stored=[])

    payload = await news_tool.handle_get_news("AAPL", "us")

    assert payload["error_code"] == "news_lookup_failed"
    assert payload["error_class"] == "ValueError"
    assert payload["data_state"] == "missing"
    assert "news" not in payload
