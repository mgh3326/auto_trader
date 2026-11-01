"""Public, unauthenticated GET with a host and path allowlist.

Every network read of the data plane goes through a ``Fetch`` callable so tests
inject fixtures. The real fetcher refuses any URL outside the public
market-data endpoints below before a socket opens, sends no credentials and
follows no redirects. There is no route to an order, account or authenticated
endpoint through it.
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from research.crypto_universe.public_http import (
    DisallowedUrlError,
    Fetch,
    FetchResponse,
    _NoRedirect,
    with_retries,
)

__all__ = [
    "ALLOWED_ENDPOINTS",
    "DisallowedUrlError",
    "Fetch",
    "FetchResponse",
    "check_url",
    "make_public_fetch",
]

# host -> allowed paths. An entry ending in "/" allows that directory and below.
ALLOWED_ENDPOINTS: dict[str, tuple[str, ...]] = {
    # Yahoo Finance chart API: daily OHLCV + dividends/splits (KR .KS/.KQ, US).
    "query1.finance.yahoo.com": ("/v8/finance/chart/",),
    "query2.finance.yahoo.com": ("/v8/finance/chart/",),
    # Binance public market-data mirror: daily klines and exchange info only.
    "data-api.binance.vision": ("/api/v3/klines", "/api/v3/exchangeInfo"),
    # Upbit quotation API: market list and daily candles only.
    "api.upbit.com": ("/v1/market/all", "/v1/candles/days"),
    # KIS public stock master files (the kr_symbol_universe source).
    "new.real.download.dws.co.kr": ("/common/master/",),
    # NASDAQ Trader symbol directory (the us_universe source).
    "www.nasdaqtrader.com": ("/dynamic/SymDir/",),
}

USER_AGENT = "Mozilla/5.0 (auto_trader research data-plane; public data)"


def _path_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    return any(
        path.startswith(entry) if entry.endswith("/") else path == entry
        for entry in allowed
    )


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
    if (
        "\\" in decoded
        or any(seg in (".", "..") for seg in decoded.split("/"))
        or decoded.count("/") != path.count("/")
    ):
        raise DisallowedUrlError(f"dot segment or encoded separator refused: {url}")
    if not _path_allowed(decoded, allowed):
        raise DisallowedUrlError(f"path not allowlisted: {parsed.hostname}{path}")


def make_public_fetch(
    *,
    min_interval: float = 0.3,
    timeout: float = 30.0,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> Fetch:
    """Allowlist check, polite spacing between requests, bounded retries."""
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    state = {"last": 0.0}

    def fetch(url: str) -> FetchResponse:
        check_url(url)
        wait = state["last"] + min_interval - time.monotonic()
        if wait > 0:
            sleep(wait)
        state["last"] = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
        try:
            with opener.open(request, timeout=timeout) as resp:
                return FetchResponse(status=resp.status, body=resp.read())
        except urllib.error.HTTPError as exc:
            return FetchResponse(status=exc.code, body=exc.read() or b"")

    return with_retries(fetch, attempts=attempts, sleep=sleep)
