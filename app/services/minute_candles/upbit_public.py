"""Unauthenticated Upbit quotation reads for the KRW one-minute collector."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from research.crypto_universe.public_http import (
    Fetch,
    FetchResponse,
    make_urllib_fetch,
    with_retries,
)

BASE_URL = "https://api.upbit.com"
MARKETS_URL = f"{BASE_URL}/v1/market/all?isDetails=false"
TICKER_PATH = "/v1/ticker"
CANDLES_PATH = "/v1/candles/minutes/1"
PAGE_SIZE = 200
PAGE_SECONDS = 60
REQUEST_INTERVAL_SECONDS = 0.15


class UpbitPublicError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Candle:
    market: str
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal


@dataclass(frozen=True, slots=True)
class CandlePage:
    candles: tuple[Candle, ...]
    received_at: datetime


class Pacer:
    """Space all Upbit quotation requests to stay below the candle group cap."""

    def __init__(
        self,
        interval: float = REQUEST_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self.clock()
        start = max(now, self._next)
        self._next = start + self.interval
        if start > now:
            self.sleep(start - now)


def _allowlisted_fetch(fetch: Fetch) -> Fetch:
    allowed = {"/v1/market/all", TICKER_PATH, CANDLES_PATH}

    def checked(url: str) -> FetchResponse:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.upbit.com"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.path not in allowed
            or parsed.fragment
        ):
            raise UpbitPublicError(
                "Upbit URL is outside the public quotation allowlist"
            )
        return fetch(url)

    return checked


class UpbitMinutePublicData:
    def __init__(
        self,
        fetch: Fetch | None = None,
        *,
        pacer: Pacer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retry_sleep: Callable[[float], None] | None = None,
    ) -> None:
        raw_fetch = with_retries(
            fetch or make_urllib_fetch(timeout=20),
            attempts=4,
            sleep=retry_sleep,
        )
        self._fetch = _allowlisted_fetch(raw_fetch)
        self._pacer = pacer or Pacer()
        self._clock = clock

    def _get_json(self, url: str, *, label: str) -> Any:
        self._pacer.wait()
        response = self._fetch(url)
        if response.status != 200:
            raise UpbitPublicError(
                f"{label}: HTTP {response.status}", status_code=response.status
            )
        try:
            return json.loads(response.body, parse_float=str, parse_int=str)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpbitPublicError(f"{label}: invalid JSON") from exc

    def markets(self) -> list[str]:
        payload = self._get_json(MARKETS_URL, label="market/all")
        if not isinstance(payload, list):
            raise UpbitPublicError("market/all: expected a JSON list")
        markets = sorted(
            {
                row["market"]
                for row in payload
                if isinstance(row, dict)
                and isinstance(row.get("market"), str)
                and row["market"].startswith("KRW-")
            }
        )
        if not markets:
            raise UpbitPublicError("market/all: no KRW markets")
        return markets

    def top_by_trade_value(self, markets: list[str], limit: int) -> list[str]:
        values: list[tuple[Decimal, str]] = []
        for offset in range(0, len(markets), 100):
            batch = markets[offset : offset + 100]
            query = urllib.parse.urlencode({"markets": ",".join(batch)}, safe=",")
            payload = self._get_json(f"{BASE_URL}{TICKER_PATH}?{query}", label="ticker")
            if not isinstance(payload, list):
                raise UpbitPublicError("ticker: expected a JSON list")
            for row in payload:
                if not isinstance(row, dict) or row.get("market") not in batch:
                    raise UpbitPublicError("ticker: malformed market row")
                try:
                    value = Decimal(str(row["acc_trade_price_24h"]))
                except (KeyError, InvalidOperation) as exc:
                    raise UpbitPublicError("ticker: invalid 24h trade value") from exc
                if not value.is_finite() or value < 0:
                    raise UpbitPublicError("ticker: invalid 24h trade value")
                values.append((value, row["market"]))
        return [
            market
            for _, market in sorted(values, key=lambda item: (-item[0], item[1]))[
                :limit
            ]
        ]

    def candles(self, market: str, before: datetime) -> CandlePage:
        query = urllib.parse.urlencode(
            {
                "market": market,
                "count": str(PAGE_SIZE),
                "to": before.astimezone(UTC).isoformat(),
            }
        )
        payload = self._get_json(f"{BASE_URL}{CANDLES_PATH}?{query}", label=market)
        received_at = self._clock().astimezone(UTC)
        if not isinstance(payload, list):
            raise UpbitPublicError(f"{market}: candles response must be a list")
        candles: list[Candle] = []
        for row in payload:
            if (
                not isinstance(row, dict)
                or row.get("market") != market
                or row.get("unit") not in ("1", 1)
            ):
                raise UpbitPublicError(f"{market}: malformed candle")
            try:
                stamp = datetime.fromisoformat(row["candle_date_time_utc"]).replace(
                    tzinfo=UTC
                )
                numbers = [
                    Decimal(str(row[key]))
                    for key in (
                        "opening_price",
                        "high_price",
                        "low_price",
                        "trade_price",
                        "candle_acc_trade_volume",
                        "candle_acc_trade_price",
                    )
                ]
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise UpbitPublicError(f"{market}: malformed candle fields") from exc
            if int(stamp.timestamp()) % PAGE_SECONDS or not all(
                n.is_finite() for n in numbers
            ):
                raise UpbitPublicError(f"{market}: invalid candle boundary or values")
            candles.append(Candle(market, stamp, *numbers))
        return CandlePage(tuple(candles), received_at)
