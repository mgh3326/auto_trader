"""Upbit public market list and 15-minute candles (hk 6036).

Reuses the hk 6035 fetch code: an allowlist that admits only
``/v1/market/all`` and ``/v1/candles/minutes/15`` (no key, no order or account
path, no redirects), exact decimal text for every number, a request pacer and
retries on 429/5xx.

Candles GET is used rather than the websocket because the frozen rules were
tested on these exchange-built bars, a run needs no long-lived process, and a
missed run leaves no gap (the same page is read again).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from research.upbit_intraday.fetch import (
    MARKETS_URL,
    PAGE_SIZE,
    FetchError,
    Pacer,
    Row,
    candles_url,
    parse_page,
)
from research.upbit_intraday.public_http import Fetch, make_urllib_fetch, with_retries
from research.upbit_intraday.rules import BAR_SECONDS

#: Upbit allows 10 candle requests per second per IP, shared with every other
#: Upbit reader on the host; this stays under 7 per second.
REQUEST_INTERVAL_SECONDS = 0.15


class MarketUnavailable(RuntimeError):
    """Upbit answered 404: it serves no candles for this market."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def slot_of(when: datetime) -> int:
    return int(when.timestamp()) // BAR_SECONDS


def slot_open(slot: int) -> datetime:
    return datetime.fromtimestamp(slot * BAR_SECONDS, UTC)


@dataclass(frozen=True)
class MarketList:
    #: ``KRW-*`` market -> its raw ``market_event`` object (``None`` if absent).
    market_events: dict[str, Any]
    observed_at: datetime


@dataclass(frozen=True)
class CandlePage:
    """One page of candles opening before ``before``.

    ``covers_from`` is the oldest slot the page speaks for: a slot at or after
    it that has no row had no trades. ``None`` means the page holds the market's
    whole history.
    """

    market: str
    rows: dict[int, Row]
    covers_from: int | None
    before_slot: int
    received_at: datetime

    def covers(self, slot: int) -> bool:
        return self.covers_from is None or slot >= self.covers_from


class UpbitPublicData:
    def __init__(
        self,
        fetch: Fetch | None = None,
        *,
        pacer: Pacer | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._fetch = fetch or with_retries(make_urllib_fetch(timeout=20.0), attempts=4)
        self._pacer = pacer or Pacer(REQUEST_INTERVAL_SECONDS)
        self._clock = clock

    def markets(self) -> MarketList:
        self._pacer.wait()
        resp = self._fetch(MARKETS_URL)
        if resp.status != 200:
            raise FetchError(f"market/all: HTTP {resp.status}")
        observed_at = self._clock()
        payload = json.loads(resp.body)
        if not isinstance(payload, list):
            raise FetchError("market/all is not a JSON list")
        events = {
            item["market"]: item.get("market_event")
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("market"), str)
            and item["market"].startswith("KRW-")
        }
        if not events:
            raise FetchError("market/all has zero KRW markets")
        return MarketList(events, observed_at)

    def candles(self, market: str, before: datetime) -> CandlePage:
        self._pacer.wait()
        resp = self._fetch(candles_url(market, before))
        if resp.status == 404:
            raise MarketUnavailable(market)
        if resp.status != 200:
            raise FetchError(f"{market}: HTTP {resp.status}")
        received_at = self._clock()
        before_slot = slot_of(before)
        rows: dict[int, Row] = {}
        for row in parse_page(market, resp.body):
            slot = slot_of(datetime.fromisoformat(row.time_utc).replace(tzinfo=UTC))
            if slot >= before_slot:
                raise FetchError(f"{market}: page is not strictly before 'to'")
            rows[slot] = row
        covers_from = min(rows) if len(rows) >= PAGE_SIZE else None
        return CandlePage(market, rows, covers_from, before_slot, received_at)
