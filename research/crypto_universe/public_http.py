"""Public-data-only HTTP GET with a host and path allowlist.

Every network call in this package goes through a ``Fetch`` callable so tests
inject fixtures and never touch the network. The real implementation refuses
any URL outside the public market-data endpoints below *before* opening a
socket: there is no way to reach an order, account or authenticated endpoint
through it. No credentials or auth headers are ever sent. Redirects are not
followed.
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

# host -> allowed paths (all public, unauthenticated market data). An entry
# ending in "/" allows that directory and below; any other entry must match
# the whole path exactly.
ALLOWED_ENDPOINTS: dict[str, tuple[str, ...]] = {
    # Binance public data archive (static zip files) and its S3 key listing.
    "data.binance.vision": ("/data/spot/",),
    "s3-ap-northeast-1.amazonaws.com": ("/data.binance.vision",),
    # Binance public market-data mirror (no trading endpoints exist there).
    "data-api.binance.vision": ("/api/v3/exchangeInfo",),
    # Upbit quotation API: market list and daily candles only.
    "api.upbit.com": ("/v1/market/all", "/v1/candles/days"),
}

USER_AGENT = "auto-trader-research-crypto-universe/1 (public data)"


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes


Fetch = Callable[[str], FetchResponse]


class DisallowedUrlError(ValueError):
    """The URL is not one of the allowlisted public data endpoints."""


def _path_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    for entry in allowed:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise DisallowedUrlError(f"non-https URL refused: {url}")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise DisallowedUrlError(f"userinfo/port refused: {url}")
    allowed = ALLOWED_ENDPOINTS.get(parsed.hostname or "")
    if allowed is None:
        raise DisallowedUrlError(f"host not allowlisted: {parsed.hostname}")
    path = parsed.path or "/"
    decoded = urllib.parse.unquote(path)
    segments = decoded.split("/")
    if (
        "\\" in decoded
        or any(seg in (".", "..") for seg in segments)
        or decoded.count("/") != path.count("/")  # an encoded "/" (%2F)
    ):
        raise DisallowedUrlError(f"dot segment or encoded separator refused: {url}")
    if not _path_allowed(decoded, allowed):
        raise DisallowedUrlError(f"path not allowlisted: {parsed.hostname}{path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def make_urllib_fetch(timeout: float = 60.0) -> Fetch:
    """Real fetcher: allowlist check, then a plain unauthenticated GET."""
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


RETRY_STATUSES = frozenset({418, 429, 500, 502, 503, 504})


def with_retries(
    fetch: Fetch,
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> Fetch:
    """Retry transient failures (429/5xx/socket errors) with doubling waits.

    A 404 or other client error returns at once: it is evidence (for example
    a delisted Upbit market), not a transient failure.
    """
    pause = sleep or time.sleep

    def fetch_with_retries(url: str) -> FetchResponse:
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                resp = fetch(url)
            except (OSError, http.client.HTTPException):
                if last_attempt:
                    raise
            else:
                if resp.status not in RETRY_STATUSES or last_attempt:
                    return resp
            pause(base_delay * (2**attempt))
        raise AssertionError("unreachable")

    return fetch_with_retries
