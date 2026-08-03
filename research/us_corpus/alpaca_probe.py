"""Bounded Alpaca market-data probe — intraday lookback measurement only.

Purpose: decide whether a v2 (intraday) corpus is feasible. Yahoo caps 1m at 30
days and 1h at 730 days, so Alpaca is the only free candidate for long intraday
history. This measures the *actual* oldest reachable bar. Nothing else.

🔴 Safety boundary, enforced in code rather than by intention:

* every request asserts `host == data.alpaca.markets` immediately before it is
  sent; anything else raises and is never transmitted,
* the trading hosts (`api.alpaca.markets`, `paper-api.alpaca.markets`) and every
  account/order/position path are refused by an explicit deny check,
* the call budget is hard-capped at 30 and the counter is checked before each
  send,
* 🔴 no intraday bars are collected or written. The probe records the oldest
  timestamp and discards the payload. "It downloads fine, may as well keep it"
  is exactly the scope creep this file refuses.

`alpaca_paper` is a BLOCK lane (ROB-1129). Market-data GETs are not an account
surface, but the full URL list is logged so the boundary is evidenced, not
asserted.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.us_corpus import config as cfg  # noqa: E402

DATA_HOST = "data.alpaca.markets"
FORBIDDEN_HOSTS = frozenset(
    {
        "api.alpaca.markets",
        "paper-api.alpaca.markets",
        "broker-api.alpaca.markets",
        "api.sandbox.alpaca.markets",
    }
)
FORBIDDEN_PATH_TOKENS = (
    "/account",
    "/orders",
    "/positions",
    "/portfolio",
    "/watchlist",
)

MAX_CALLS = 30
SYMBOLS = ("AAPL", "ORCL", "SMCI")
TIMEFRAMES = ("1Min", "1Hour")
# Deliberately older than any US equity electronic tape we expect Alpaca to
# hold, so the first returned bar is the true floor rather than our guess.
PROBE_START = "2000-01-01T00:00:00Z"


class ProbeBudgetExceeded(RuntimeError):
    pass


class HostBoundaryViolation(RuntimeError):
    pass


def assert_data_host(url: str) -> None:
    """🔴 Called immediately before every send. Refuses rather than warns."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in FORBIDDEN_HOSTS:
        raise HostBoundaryViolation(f"trading host refused: {host}")
    if host != DATA_HOST:
        raise HostBoundaryViolation(f"host not allowlisted: {host!r} != {DATA_HOST}")
    if parsed.scheme != "https":
        raise HostBoundaryViolation(f"non-https refused: {parsed.scheme}")
    lowered = parsed.path.lower()
    for token in FORBIDDEN_PATH_TOKENS:
        if token in lowered:
            raise HostBoundaryViolation(f"account-surface path refused: {parsed.path}")


def load_credentials() -> tuple[str, str]:
    """Read the existing Alpaca paper credentials. 🔴 Values are never printed."""
    key = os.environ.get("ALPACA_PAPER_API_KEY")
    secret = os.environ.get("ALPACA_PAPER_API_SECRET")
    if key and secret:
        return key, secret

    env_path = Path("/Users/mgh3326/work/auto_trader/.env.prod")
    if not env_path.exists():
        raise RuntimeError(f"no credential source: {env_path} missing")
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    key = values.get("ALPACA_PAPER_API_KEY")
    secret = values.get("ALPACA_PAPER_API_SECRET")
    if not key or not secret:
        raise RuntimeError("ALPACA_PAPER_API_KEY/SECRET not present in .env.prod")
    return key, secret


class Probe:
    def __init__(self, key: str, secret: str) -> None:
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "accept": "application/json",
        }
        self.calls = 0
        self.urls: list[str] = []
        self.hosts: set[str] = set()
        self.rate_limit_headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, str]) -> httpx.Response:
        if self.calls >= MAX_CALLS:
            raise ProbeBudgetExceeded(f"probe budget {MAX_CALLS} exhausted")
        assert_data_host(url)  # 🔴 immediately before the send
        self.calls += 1
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, params=params, headers=self._headers)
        self.urls.append(str(response.request.url))
        self.hosts.add(urlparse(str(response.request.url)).hostname or "")
        for name, value in response.headers.items():
            if name.lower().startswith("x-ratelimit"):
                self.rate_limit_headers[name] = value
        return response

    def oldest_bar(self, symbol: str, timeframe: str, feed: str) -> dict[str, object]:
        """Ask for the single earliest bar. sort=asc + limit=1 makes the first
        row the lookback floor, so one call answers the question per pair."""
        url = f"https://{DATA_HOST}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "start": PROBE_START,
            "limit": "1",
            "sort": "asc",
            "adjustment": "raw",
            "feed": feed,
        }
        result: dict[str, object] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "feed": feed,
        }
        try:
            response = self.get(url, params)
        except (ProbeBudgetExceeded, HostBoundaryViolation) as exc:
            result["oldest"] = "UNKNOWN"
            result["reason"] = f"{type(exc).__name__}: {exc}"
            return result

        result["http_status"] = response.status_code
        if response.status_code != 200:
            result["oldest"] = "UNKNOWN"
            result["reason"] = f"HTTP {response.status_code}: {response.text[:200]}"
            return result

        payload = response.json()
        bars = payload.get("bars") or []
        if not bars:
            result["oldest"] = "UNKNOWN"
            result["reason"] = "200 OK but zero bars returned for the probe window"
            return result
        # 🔴 Only the timestamp is retained. The bar payload is discarded —
        # collecting intraday data is out of scope (v2, separate approval).
        result["oldest"] = bars[0].get("t")
        result["reason"] = "measured"
        return result


def main() -> int:
    cfg.PROBE_DIR.mkdir(parents=True, exist_ok=True)
    key, secret = load_credentials()
    probe = Probe(key, secret)

    results: list[dict[str, object]] = []
    # The free tier serves IEX; sip needs a paid subscription. Probe iex first
    # and try sip once, so the report distinguishes "no history" from
    # "not entitled" instead of collapsing both into UNKNOWN.
    for timeframe in TIMEFRAMES:
        for symbol in SYMBOLS:
            results.append(probe.oldest_bar(symbol, timeframe, "iex"))
    for timeframe in TIMEFRAMES:
        results.append(probe.oldest_bar("AAPL", timeframe, "sip"))

    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe_calls": probe.calls,
        "max_calls": MAX_CALLS,
        "hosts_contacted": sorted(h for h in probe.hosts if h),
        "trading_host_contacted": False,
        "account_or_order_endpoints": 0,
        "intraday_data_collected": False,
        "rate_limit_headers": probe.rate_limit_headers,
        "urls": probe.urls,
        "results": results,
    }
    out = cfg.PROBE_DIR / "alpaca_lookback.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
