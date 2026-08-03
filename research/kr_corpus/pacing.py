"""Single-process request accounting, host restriction, and rate pacing."""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

ALLOWED_PYKRX_HOSTS = frozenset({"data.krx.co.kr", "fchart.stock.naver.com"})
_BLOCK_SIGNAL_STATUSES = frozenset({403, 429, 503})


class RequestBudgetExceeded(RuntimeError):
    """A call would exceed the signed MAX_REQUESTS limit."""


class UnexpectedSourceHost(RuntimeError):
    """pykrx attempted to access a host outside the signed source product."""


@dataclass(frozen=True)
class RequestEvent:
    ordinal: int
    method: str
    host: str
    path: str
    status_code: int | None


@dataclass
class RequestPacer:
    """Patch ``requests.Session.request`` while the pykrx adapter is active.

    The patch captures every network request made internally by pykrx,
    including login/refresh requests.  It never records query parameters,
    request bodies, headers, or response bodies, all of which may contain
    sensitive material.
    """

    min_interval_sec: float
    max_requests: int
    on_event: Callable[[RequestEvent], None] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    request_count: int = 0
    blocked_signal_seen: bool = False
    blocked_statuses: list[int] = field(default_factory=list)
    _last_started_at: float | None = None

    def _wait_for_slot(self) -> None:
        if self._last_started_at is None:
            return
        elapsed = self.monotonic() - self._last_started_at
        remaining = self.min_interval_sec - elapsed
        if remaining > 0:
            self.sleeper(remaining)

    @contextlib.contextmanager
    def patch_requests(self) -> Iterator[None]:
        """Install the single-process request guard for the enclosed work."""
        import requests

        original = requests.sessions.Session.request

        def guarded_request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if host not in ALLOWED_PYKRX_HOSTS:
                raise UnexpectedSourceHost(f"pykrx attempted unexpected host: {host}")
            if self.request_count >= self.max_requests:
                raise RequestBudgetExceeded(
                    "MAX_REQUESTS would be exceeded before this source request"
                )
            self._wait_for_slot()
            self._last_started_at = self.monotonic()
            self.request_count += 1
            event = RequestEvent(
                ordinal=self.request_count,
                method=method.upper(),
                host=host,
                path=parsed.path,
                status_code=None,
            )
            try:
                response = original(session, method, url, **kwargs)
            except Exception:
                if self.on_event is not None:
                    self.on_event(event)
                raise

            status_code = getattr(response, "status_code", None)
            event = RequestEvent(
                ordinal=event.ordinal,
                method=event.method,
                host=event.host,
                path=event.path,
                status_code=status_code,
            )
            if status_code in _BLOCK_SIGNAL_STATUSES:
                self.blocked_signal_seen = True
                self.blocked_statuses.append(status_code)
            if self.on_event is not None:
                self.on_event(event)
            return response

        requests.sessions.Session.request = guarded_request
        try:
            yield
        finally:
            requests.sessions.Session.request = original


@dataclass(frozen=True)
class RequestProjection:
    """Conservative pre-fetch request budget calculation."""

    requests_already_observed: int
    session_count: int
    markets_count: int
    lifecycle_master_upper_bound: int
    max_wall_clock_hours: int

    @property
    def membership_requests(self) -> int:
        return self.session_count * self.markets_count

    @property
    def ohlcv_requests(self) -> int:
        # One adjusted pykrx/Naver history call per ticker is the source-product
        # plan.  The lifecycle master is a budget upper bound only, never a
        # membership or common-stock classifier.
        return self.lifecycle_master_upper_bound

    @property
    def maximum_session_refresh_requests(self) -> int:
        # pykrx's authenticated KRXSession expires after one hour; a refresh
        # uses warmup GET + login-page GET + login POST.  Initial auth is part
        # of requests_already_observed, so this is only future conservative
        # allowance.
        return math.ceil(self.max_wall_clock_hours) * 3

    @property
    def total(self) -> int:
        return (
            self.requests_already_observed
            + self.membership_requests
            + self.ohlcv_requests
            + self.maximum_session_refresh_requests
        )
