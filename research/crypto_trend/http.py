"""Public, unauthenticated GET restricted to the Binance USD-M kline archive.

This allowlist is deliberately separate from ``research.crypto_universe``'s
``public_http``: that one covers spot paths and must not be widened for this
study. Here only the futures/um monthly kline zips and their ``.CHECKSUM``
files are reachable. Every URL is refused before a socket opens, no
credentials or auth headers are sent, and redirects are not followed.
"""

from __future__ import annotations

import http.client
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

HOST = "data.binance.vision"
USER_AGENT = "auto-trader-research-crypto-trend/1 (public data)"

_KLINE_PATH = re.compile(
    r"/data/futures/um/monthly/klines/(?P<symbol>[A-Z0-9]{2,20})/(?P<interval>4h|1d)/"
    r"(?P=symbol)-(?P=interval)-\d{4}-\d{2}\.zip(?:\.CHECKSUM)?"
)


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes


Fetch = Callable[[str], FetchResponse]


class DisallowedUrlError(ValueError):
    """The URL is not a futures/um kline archive file on the allowed host."""


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise DisallowedUrlError(f"non-https URL refused: {url}")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise DisallowedUrlError(f"userinfo/port refused: {url}")
    if parsed.hostname != HOST:
        raise DisallowedUrlError(f"host not allowlisted: {parsed.hostname}")
    if parsed.query or parsed.fragment:
        raise DisallowedUrlError(f"query/fragment refused: {url}")
    path = parsed.path
    if "%" in path or "\\" in path:
        raise DisallowedUrlError(f"encoded or backslash path refused: {url}")
    if any(seg in (".", "..") for seg in path.split("/")):
        raise DisallowedUrlError(f"dot segment refused: {url}")
    if _KLINE_PATH.fullmatch(path) is None:
        raise DisallowedUrlError(f"path not allowlisted: {path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def make_urllib_fetch(timeout: float = 60.0) -> Fetch:
    """Real fetcher: allowlist check first, then a plain GET with no redirects."""
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )

    def fetch(url: str) -> FetchResponse:
        check_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(request, timeout=timeout) as resp:
                return FetchResponse(status=resp.status, body=resp.read())
        except urllib.error.HTTPError as exc:
            return FetchResponse(status=exc.code, body=exc.read() or b"")

    return fetch


_RETRY_STATUSES = frozenset({418, 429, 500, 502, 503, 504})


def with_retries(fetch: Fetch, *, attempts: int = 5, base_delay: float = 1.0) -> Fetch:
    """Retry transient statuses and socket errors; a 404 returns at once."""

    def fetch_with_retries(url: str) -> FetchResponse:
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = fetch(url)
            except (OSError, http.client.HTTPException):
                if last:
                    raise
            else:
                if resp.status not in _RETRY_STATUSES or last:
                    return resp
            time.sleep(base_delay * (2**attempt))
        raise AssertionError("unreachable")

    return fetch_with_retries
