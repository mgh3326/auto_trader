"""Polite HTTP client for FnGuide public CompanyInfo pages (hk 6508).

* Host allowlist: only ``wcomp.fnguide.com`` over https. The historical
  ``comp.fnguide.com`` URL answers 302 to it; we request the target directly, so
  ``follow_redirects=False`` stays and any other redirect is recorded, not followed.
* Pacing: at most ``concurrency`` (<= 2) requests in flight and one request start
  per ``min_interval`` seconds across the whole client.
* Retries only on timeout, transport error and 5xx, bounded by ``retries``.
  4xx and 3xx are final and recorded as-is.
* The User-Agent names the client generically; no contact detail is sent.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

ALLOWED_HOST = "wcomp.fnguide.com"
BASE_URL = f"https://{ALLOWED_HOST}/CompanyInfo/{{page}}?cmp_cd={{code}}"
USER_AGENT = "at-research-collector/1.0"
MAX_CONCURRENCY = 2
MAX_RESPONSE_BYTES = 2_000_000
_CODE_RE = re.compile(r"\d{6}")

_FETCH_KINDS = frozenset(
    {
        "timeout",
        "transport",
        "http_4xx",
        "http_5xx",
        "redirect_refused",
        "host_refused",
        "too_large",
    }
)


class FetchError(Exception):
    """A fetch that produced no usable page. ``kind`` is a closed code, never a body."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        if kind not in _FETCH_KINDS:
            raise ValueError(f"unknown fetch error kind: {kind}")
        super().__init__(kind)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    text: str
    fetched_at: datetime


def build_url(page: str, code: str) -> str:
    if not _CODE_RE.fullmatch(code):
        raise ValueError("symbol must be six digits")
    return BASE_URL.format(page=page, code=code)


class FnguideClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = MAX_CONCURRENCY,
        min_interval: float = 1.0,
        retries: int = 2,
        timeout: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pace_lock = asyncio.Lock()
        self._min_interval = min_interval
        self._next_start = 0.0
        self._retries = retries
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.9"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FnguideClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _pace(self) -> None:
        async with self._pace_lock:
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self._min_interval
            if start > now:
                await self._sleep(start - now)

    async def fetch_page(self, page: str, code: str) -> FetchResult:
        url = build_url(page, code)
        async with self._semaphore:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await self._get_once(url)
                except FetchError as error:
                    retryable = error.kind in {"timeout", "transport", "http_5xx"}
                    if not retryable or attempt > self._retries:
                        raise
                await self._sleep(min(2.0, 0.5 * attempt))

    async def _get_once(self, url: str) -> FetchResult:
        request = self._http.build_request("GET", url)
        if (
            request.url.scheme != "https"
            or request.url.host != ALLOWED_HOST
            or request.url.port not in (None, 443)
        ):
            raise FetchError("host_refused")
        await self._pace()
        try:
            response = await self._http.send(request, stream=False)
        except httpx.TimeoutException as error:
            raise FetchError("timeout") from error
        except httpx.TransportError as error:
            raise FetchError("transport") from error
        status = response.status_code
        if 500 <= status:
            raise FetchError("http_5xx", status)
        if 400 <= status:
            raise FetchError("http_4xx", status)
        if 300 <= status:
            raise FetchError("redirect_refused", status)
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise FetchError("too_large", status)
        return FetchResult(
            url=url,
            text=body.decode("utf-8", "replace"),
            fetched_at=datetime.now(UTC),
        )
