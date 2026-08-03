"""Read-only Alpaca market-data client, pinned to the data host.

Boundary (brief §4), enforced rather than promised:

* `assert_data_host()` runs immediately before **every** request. Anything that
  is not `data.alpaca.markets` raises, and the trading hosts are additionally
  named in an explicit deny-list so a typo cannot silently reach them.
* `CONTACTED_HOSTS` accumulates every host actually contacted, so the final
  report can prove what was called instead of asserting what was not.
* Only GET is implemented. There is no code path here that can place an order,
  read an account, or mutate anything.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import config

# Every host this process actually contacted. Reported verbatim.
CONTACTED_HOSTS: set[str] = set()

BARS_ENDPOINT = f"https://{config.DATA_HOST}/v2/stocks/bars"


class AlpacaCredentialsMissing(RuntimeError):
    """Raised when no read-only market-data credentials are available."""


class ForbiddenHostError(RuntimeError):
    """Raised on any attempt to contact a non-data Alpaca host."""


def assert_data_host(url: str) -> str:
    """Assert `url` targets the data host. Returns the host on success."""
    host = urlparse(url).hostname or ""
    if host in config.FORBIDDEN_HOSTS:
        raise ForbiddenHostError(
            f"refusing to contact trading/broker host {host!r}. "
            "This corpus builder is read-only market data."
        )
    if host != config.DATA_HOST:
        raise ForbiddenHostError(
            f"host {host!r} is not the pinned data host {config.DATA_HOST!r}"
        )
    if urlparse(url).scheme != "https":
        raise ForbiddenHostError(f"refusing non-https URL: {url!r}")
    return host


# Credential files this builder must never read. `.env.prod` carries the full
# production surface (DB URLs, trading base URLs, other brokers) and the dev
# `.env` is likewise off-limits, so both are denied by name rather than merely
# not consulted.
FORBIDDEN_ENV_FILES = ("/.env.prod", "/.env.dev", "/.env")

SANCTIONED_ENV_FILE = (
    "/Users/mgh3326/services/auto_trader/shared/.env.alpaca-data-readonly.native"
)


class ForbiddenEnvFileError(RuntimeError):
    """Raised when credentials are sourced from a denied env file."""


def assert_env_file_allowed(path: str) -> str:
    """Reject any credential file outside the sanctioned read-only one."""
    resolved = os.path.abspath(path)
    for denied in FORBIDDEN_ENV_FILES:
        if resolved.endswith(denied):
            raise ForbiddenEnvFileError(
                f"refusing to read credentials from {resolved}: this builder is "
                "restricted to the dedicated read-only market-data env file."
            )
    return resolved


def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file. Values are never logged or echoed."""
    assert_env_file_allowed(path)
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_credentials() -> tuple[str, str]:
    """Resolve read-only market-data credentials.

    Source order, narrowest first:
      1. `ENV_FILE` -- must point at the sanctioned read-only file; `.env.prod`,
         `.env.dev` and `.env` are refused by `assert_env_file_allowed`.
      2. the sanctioned file at its default path.
      3. the process environment.

    Creating a new secret is forbidden, so absence raises and the caller reports
    BLOCKED_PRECONDITION rather than inventing a fallback.
    """
    sources: list[dict[str, str]] = []

    env_file = os.environ.get("ENV_FILE")
    if env_file:
        sources.append(load_env_file(env_file))
    elif os.path.exists(SANCTIONED_ENV_FILE):
        sources.append(load_env_file(SANCTIONED_ENV_FILE))
    sources.append(dict(os.environ))

    def pick(*names: str) -> str | None:
        for source in sources:
            for name in names:
                if source.get(name):
                    return source[name]
        return None

    key = pick("ALPACA_DATA_API_KEY", "ALPACA_PAPER_API_KEY")
    secret = pick("ALPACA_DATA_API_SECRET", "ALPACA_PAPER_API_SECRET")
    if not key or not secret:
        raise AlpacaCredentialsMissing(
            "no Alpaca market-data credentials available (looked for "
            "ALPACA_DATA_API_KEY/_SECRET and ALPACA_PAPER_API_KEY/_SECRET in "
            f"ENV_FILE, {SANCTIONED_ENV_FILE}, and the process environment). "
            "Creating a new secret is forbidden, so this is a precondition to "
            "escalate, not to work around."
        )
    return key, secret


@dataclass
class RateLimiter:
    """Fixed floor between requests. Never speeds up, even after a quiet spell."""

    min_interval_sec: float = config.MIN_REQUEST_INTERVAL_SEC
    _last: float = field(default=0.0, repr=False)

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)
        self._last = time.monotonic()


@dataclass
class RequestCounter:
    """Hard request budget. Raises rather than silently exceeding §1."""

    max_requests: int = config.MAX_REQUESTS
    count: int = 0
    rate_429: int = 0

    def spend(self, n: int = 1) -> None:
        if self.count + n > self.max_requests:
            raise RuntimeError(
                f"request budget exhausted ({self.count}/{self.max_requests}). "
                "Raising the cap is not this worker's call."
            )
        self.count += n


@dataclass
class PageChain:
    """Per-symbol pagination evidence (brief §3.9).

    Records how many pages were consumed and *why* the chain ended, so a
    truncated page walk cannot masquerade as a data gap.
    """

    symbol: str
    timeframe: str
    pages: int = 0
    rows: int = 0
    last_token: str | None = None
    termination: str = "unstarted"  # "null_next_token" | "budget" | "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pages": self.pages,
            "rows": self.rows,
            "last_token": self.last_token,
            "termination": self.termination,
            "complete": self.termination == "null_next_token",
        }


class AlpacaDataClient:
    """Minimal GET-only client for `/v2/stocks/bars`."""

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        counter: RequestCounter | None = None,
        session: Any | None = None,
    ) -> None:
        import requests

        key, secret = load_credentials()
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
            }
        )
        self.limiter = limiter or RateLimiter()
        self.counter = counter or RequestCounter()

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        host = assert_data_host(url)
        CONTACTED_HOSTS.add(host)

        backoff = 1.0
        for _attempt in range(8):
            self.limiter.wait()
            self.counter.spend()
            response = self._session.get(url, params=params, timeout=30)
            if response.status_code == 429:
                self.counter.rate_429 += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)  # back off only; never speed up
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"giving up after repeated 429s: {url} {params}")

    def fetch_bars_page(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: str,
        *,
        page_token: str | None = None,
        limit: int = 10_000,
    ) -> dict[str, Any]:
        """Fetch one page of bars for one or more symbols."""
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "limit": limit,
            "adjustment": "all",
            "feed": config.DATA_FEED,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        return self._get(BARS_ENDPOINT, params)

    def iter_bars(
        self, symbols: list[str], timeframe: str, start: str, end: str
    ) -> Iterator[tuple[dict[str, Any], PageChain]]:
        """Walk the full pagination chain, yielding each page with its evidence.

        The chain is only marked complete when the API returns a null
        `next_page_token` -- §3.9. Any other exit is recorded as such.
        """
        chain = PageChain(symbol=",".join(symbols), timeframe=timeframe)
        token: str | None = None
        while True:
            try:
                payload = self.fetch_bars_page(
                    symbols, timeframe, start, end, page_token=token
                )
            except Exception:
                chain.termination = "error"
                raise
            chain.pages += 1
            chain.rows += sum(len(v) for v in (payload.get("bars") or {}).values())
            token = payload.get("next_page_token")
            chain.last_token = token
            yield payload, chain
            if not token:
                chain.termination = "null_next_token"
                return


def probe_multi_symbol_form(
    client: AlpacaDataClient, symbols: list[str]
) -> dict[str, Any]:
    """Measure whether the bars endpoint honours a comma-separated symbol list.

    Brief §3.1 forbids assuming this. The measured values feed the budget
    projection, so a wrong assumption here moves the request count by an order
    of magnitude.
    """
    payload = client.fetch_bars_page(
        symbols, "1Hour", "2024-01-02T00:00:00Z", "2024-01-05T00:00:00Z"
    )
    bars = payload.get("bars") or {}
    returned = sorted(bars.keys())
    per_symbol_rows = {sym: len(rows) for sym, rows in bars.items()}
    return {
        "symbols_requested": symbols,
        "symbols_returned": returned,
        "multi_symbol_form_supported": len(returned) > 1,
        "symbols_per_request_measured": len(returned),
        "bars_returned_total": sum(per_symbol_rows.values()),
        "per_symbol_rows": per_symbol_rows,
        "next_page_token_present": bool(payload.get("next_page_token")),
        "raw_keys": sorted(payload.keys()),
    }
