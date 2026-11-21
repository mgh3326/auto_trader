"""Public-data-only HTTP GET for Upbit quotation endpoints (hk 6035).

Same contract as ``research.crypto_universe.public_http`` with a narrower
allowlist: the KRW market list and 15-minute candles only. The URL is checked
*before* a socket is opened, so no order, account or authenticated endpoint can
be reached through this module. No credentials or auth headers are sent, and
redirects are not followed.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

from research.crypto_universe.public_http import (
    DisallowedUrlError,
    Fetch,
    FetchResponse,
    with_retries,
)

__all__ = [
    "ALLOWED_ENDPOINTS",
    "DisallowedUrlError",
    "Fetch",
    "FetchResponse",
    "check_url",
    "make_urllib_fetch",
    "with_retries",
]

ALLOWED_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "api.upbit.com": ("/v1/market/all", "/v1/candles/minutes/15"),
}

USER_AGENT = "auto-trader-research-upbit-intraday/1 (public data)"


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
        or decoded.count("/") != path.count("/")
    ):
        raise DisallowedUrlError(f"dot segment or encoded separator refused: {url}")
    if decoded not in allowed:
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
