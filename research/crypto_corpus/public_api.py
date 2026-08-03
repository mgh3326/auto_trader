"""Unsigned public HTTPS transport for the two permitted market-data APIs."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .artifacts import ArtifactStore
from .constants import (
    BINANCE_MIN_REQUEST_INTERVAL_SECONDS,
    MAX_REQUESTS,
    UPBIT_MIN_REQUEST_INTERVAL_SECONDS,
)


class RequestBudgetExceeded(RuntimeError):
    """Raised before a network call that would exceed the signed cap."""


class TransportFailure(RuntimeError):
    """A public API could not be reached or returned unreadable JSON."""


@dataclass(frozen=True)
class ApiResponse:
    url: str
    venue: str
    status: int | None
    body: bytes
    payload: Any | None
    error: str | None
    rate_limited: bool

    @property
    def ok(self) -> bool:
        return (
            self.status is not None and 200 <= self.status < 300 and self.error is None
        )


RawOpener = Callable[[Request, float], tuple[int, bytes]]


def _default_open(request: Request, timeout_seconds: float) -> tuple[int, bytes]:
    """Call public APIs without proxy-env, credentials, or signed headers."""
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:
        return int(response.status), response.read()


class PublicApiClient:
    """A sequential, append-logged public-data client.

    It intentionally has no credential input, no environment lookup, and no
    retry path.  A rate-limit or block signal is surfaced to the builder so it
    can checkpoint and terminate honestly instead of increasing speed.
    """

    def __init__(
        self,
        store: ArtifactStore,
        *,
        opener: RawOpener = _default_open,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.opener = opener
        self.sleep = sleep
        self.monotonic = monotonic
        self.timeout_seconds = timeout_seconds
        self._last_request_at: dict[str, float] = {}
        self._requests_actual = self._count_existing_request_log()

    @property
    def requests_actual(self) -> int:
        return self._requests_actual

    def _count_existing_request_log(self) -> int:
        path = self.store.root / "control/request-log.jsonl"
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    @staticmethod
    def _minimum_interval(venue: str) -> float:
        if venue == "upbit_krw":
            return UPBIT_MIN_REQUEST_INTERVAL_SECONDS
        if venue == "binance_usdt_spot":
            return BINANCE_MIN_REQUEST_INTERVAL_SECONDS
        raise ValueError(f"unsupported public venue {venue!r}")

    def _reserve_request(self, venue: str, url: str) -> None:
        if self._requests_actual >= MAX_REQUESTS:
            raise RequestBudgetExceeded(
                f"request cap {MAX_REQUESTS} reached before {venue} request"
            )
        previous = self._last_request_at.get(venue)
        now = self.monotonic()
        if previous is not None:
            remaining = self._minimum_interval(venue) - (now - previous)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_at[venue] = self.monotonic()
        # The append happens *before* I/O.  A crash in flight can overcount by
        # one request, which is conservative and never weakens the cap.
        self.store.append_jsonl(
            "control/request-log.jsonl",
            {"venue": venue, "url": url, "request_number": self._requests_actual + 1},
        )
        self._requests_actual += 1

    @staticmethod
    def _is_rate_limited(status: int | None, body: bytes) -> bool:
        if status in {418, 429}:
            return True
        message = body.decode("utf-8", errors="replace").lower()
        signals = (
            "rate limit",
            "too many requests",
            "too much request weight",
            "banned",
            "ip has been auto-banned",
        )
        return any(signal in message for signal in signals)

    def get_json(self, venue: str, url: str) -> ApiResponse:
        """Make one unsigned GET request and retain a raw response body."""
        self._reserve_request(venue, url)
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "crypto-corpus-v1-research/1.0",
            },
            method="GET",
        )
        try:
            status, body = self.opener(request, self.timeout_seconds)
        except HTTPError as exc:
            status = exc.code
            body = exc.read()
        except URLError as exc:
            return ApiResponse(
                url=url,
                venue=venue,
                status=None,
                body=b"",
                payload=None,
                error=f"transport_error:{exc.reason}",
                rate_limited=False,
            )
        except OSError as exc:
            return ApiResponse(
                url=url,
                venue=venue,
                status=None,
                body=b"",
                payload=None,
                error=f"transport_error:{exc}",
                rate_limited=False,
            )

        rate_limited = self._is_rate_limited(status, body)
        if not 200 <= status < 300:
            return ApiResponse(
                url=url,
                venue=venue,
                status=status,
                body=body,
                payload=None,
                error=f"http_status:{status}",
                rate_limited=rate_limited,
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            return ApiResponse(
                url=url,
                venue=venue,
                status=status,
                body=body,
                payload=None,
                error=f"invalid_json:{exc.msg}",
                rate_limited=rate_limited,
            )
        return ApiResponse(
            url=url,
            venue=venue,
            status=status,
            body=body,
            payload=payload,
            error=None,
            rate_limited=rate_limited,
        )
