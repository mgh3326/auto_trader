"""Upbit KRW daily candles and delisted-market probes (public quotation API).

Upbit publishes no historical data archive. Its quotation API serves candles
only for markets it still lists; a delisted market answers HTTP 404
``Code not found``. So delisted Upbit pairs are obtainable only if a probe
returns candles, and every probe's request and outcome are recorded as
coverage evidence rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .public_http import Fetch

MARKETS_URL = "https://api.upbit.com/v1/market/all?isDetails=true"
DAYS_URL = "https://api.upbit.com/v1/candles/days"
PAGE_SIZE = 200
MIN_REQUEST_INTERVAL_SECONDS = 0.3
MAX_PAGES_PER_MARKET = 40  # 8,000 days > Upbit's whole history

# Markets Upbit is known to have delisted. Each one is probed; the probe
# result, not this list, decides coverage.
DEFAULT_DELISTED_PROBES: tuple[str, ...] = ("KRW-NPXS", "KRW-LUNA")


class UpbitDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpbitCandle:
    day: date  # candle_date_time_utc (Upbit day = 00:00 UTC / 09:00 KST)
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_volume: str


@dataclass(frozen=True)
class ProbeResult:
    market: str
    url: str
    http_status: int
    body_sha256: str
    candles_returned: int
    obtainable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "url": self.url,
            "http_status": self.http_status,
            "body_sha256": self.body_sha256,
            "candles_returned": self.candles_returned,
            "obtainable": self.obtainable,
        }


class Pacer:
    """Keep at least ``interval`` seconds between Upbit requests."""

    def __init__(
        self,
        interval: float = MIN_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()


def krw_markets(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise UpbitDataError("market/all is not a JSON list")
    out = {
        item["market"]
        for item in payload
        if isinstance(item, dict)
        and isinstance(item.get("market"), str)
        and item["market"].startswith("KRW-")
    }
    if not out:
        raise UpbitDataError("market/all has zero KRW markets")
    return sorted(out)


def _num(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UpbitDataError(f"non-numeric candle field {value!r}")
    return repr(value) if isinstance(value, float) else str(value)


def _parse_candles(market: str, payload: Any) -> list[UpbitCandle]:
    if not isinstance(payload, list):
        raise UpbitDataError(f"{market}: candles response is not a list")
    out: list[UpbitCandle] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("market") != market:
            raise UpbitDataError(f"{market}: malformed candle {item!r}")
        stamp = datetime.fromisoformat(item["candle_date_time_utc"]).replace(tzinfo=UTC)
        out.append(
            UpbitCandle(
                day=stamp.date(),
                open=_num(item["opening_price"]),
                high=_num(item["high_price"]),
                low=_num(item["low_price"]),
                close=_num(item["trade_price"]),
                volume=_num(item["candle_acc_trade_volume"]),
                quote_volume=_num(item["candle_acc_trade_price"]),
            )
        )
    return out


def days_url(market: str, to: date | None) -> str:
    query = {"market": market, "count": str(PAGE_SIZE)}
    if to is not None:
        query["to"] = f"{to.isoformat()}T00:00:00Z"  # exclusive upper bound
    return DAYS_URL + "?" + urllib.parse.urlencode(query)


def fetch_daily_candles(
    fetch: Fetch, market: str, coverage_end: date, pacer: Pacer
) -> list[UpbitCandle]:
    """Every daily candle of ``market`` up to and including ``coverage_end``."""
    candles: dict[date, UpbitCandle] = {}
    to: date | None = coverage_end + timedelta(days=1)
    for _ in range(MAX_PAGES_PER_MARKET):
        pacer.wait()
        url = days_url(market, to)
        resp = fetch(url)
        if resp.status != 200:
            raise UpbitDataError(f"{market}: HTTP {resp.status}")
        page = _parse_candles(market, json.loads(resp.body))
        for candle in page:
            if candle.day <= coverage_end:
                candles[candle.day] = candle
        if len(page) < PAGE_SIZE:
            return [candles[d] for d in sorted(candles)]
        earliest = min(c.day for c in page)
        if to is not None and earliest >= to:
            raise UpbitDataError(f"{market}: pagination did not advance")
        to = earliest
    raise UpbitDataError(f"{market}: more than {MAX_PAGES_PER_MARKET} pages")


def probe_delisted(
    fetch: Fetch, markets: Iterable[str], pacer: Pacer
) -> list[ProbeResult]:
    """One public candles request per candidate delisted market."""
    results: list[ProbeResult] = []
    for market in markets:
        pacer.wait()
        url = DAYS_URL + "?" + urllib.parse.urlencode({"market": market, "count": "1"})
        resp = fetch(url)
        returned = 0
        if resp.status == 200:
            try:
                returned = len(_parse_candles(market, json.loads(resp.body)))
            except (UpbitDataError, ValueError, KeyError):
                returned = 0
        results.append(
            ProbeResult(
                market=market,
                url=url,
                http_status=resp.status,
                body_sha256=hashlib.sha256(resp.body).hexdigest(),
                candles_returned=returned,
                obtainable=returned > 0,
            )
        )
    return results
