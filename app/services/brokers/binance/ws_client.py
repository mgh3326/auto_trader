"""ROB-285 — Binance public WS client (combined streams).

Subscribes to a combined-stream URL like::

    wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker

Yields normalized events. In-progress klines (``x: False``) are dropped
per parent plan §B.3; only closed klines (``x: True``) are emitted.

Open items lean adopted (per ROB-285 plan §Open items):
- #1 SDK WS vs websockets library (Task 10): use ``websockets`` directly.
- #2 combined-stream URL vs SUBSCRIBE (Task 10): URL query param.
- #4 WS test fixture (Task 10): ``websockets.serve`` local test server.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import PUBLIC_HOSTS


@dataclass(frozen=True, slots=True)
class KlineEvent:
    symbol: str
    interval: str
    open_time: dt.datetime
    close_time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    is_closed: bool


@dataclass(frozen=True, slots=True)
class BookTickerEvent:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    received_at: dt.datetime


WsEvent = KlineEvent | BookTickerEvent


def _assert_url_host_allowed(url: str, *, allowed: frozenset[str]) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    # Local test override: 127.0.0.1 over plain ``ws`` is acceptable —
    # unit tests inject a local ``websockets.serve`` server. Production
    # WS URLs are always ``wss://`` to an allowlisted host.
    if host == "127.0.0.1" and parsed.scheme == "ws":
        return
    if host not in allowed:
        raise BinanceLiveHostBlocked(
            f"WS host {host!r} is not in PUBLIC_HOSTS. "
            "Allowed: " + ", ".join(sorted(allowed))
        )


class BinancePublicWSClient:
    """Read-only WS subscriber for Binance combined streams."""

    def __init__(self, *, url: str) -> None:
        _assert_url_host_allowed(url, allowed=PUBLIC_HOSTS)
        self._url = url
        self._ws: Any = None

    async def __aenter__(self) -> BinancePublicWSClient:
        self._ws = await websockets.connect(self._url, ping_interval=20)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def events(self, *, stop_after: int | None = None) -> AsyncIterator[WsEvent]:
        emitted = 0
        assert self._ws is not None, "BinancePublicWSClient not connected"
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = msg.get("data") or {}
            event_type = data.get("e")
            ev: WsEvent
            if event_type == "kline":
                k = data["k"]
                if not k.get("x"):
                    # Drop in-progress kline (parent plan §B.3 lock).
                    continue
                ev = KlineEvent(
                    symbol=k["s"],
                    interval=k["i"],
                    open_time=dt.datetime.fromtimestamp(k["t"] / 1000.0, tz=dt.UTC),
                    close_time=dt.datetime.fromtimestamp(k["T"] / 1000.0, tz=dt.UTC),
                    open=Decimal(k["o"]),
                    high=Decimal(k["h"]),
                    low=Decimal(k["l"]),
                    close=Decimal(k["c"]),
                    base_volume=Decimal(k["v"]),
                    quote_volume=Decimal(k["q"]),
                    trade_count=int(k["n"]),
                    is_closed=True,
                )
            elif data.get("u") is not None and "b" in data and "a" in data:
                # bookTicker stream payload shape (no "e" field).
                ev = BookTickerEvent(
                    symbol=data["s"],
                    bid_price=Decimal(data["b"]),
                    bid_qty=Decimal(data["B"]),
                    ask_price=Decimal(data["a"]),
                    ask_qty=Decimal(data["A"]),
                    received_at=dt.datetime.now(tz=dt.UTC),
                )
            else:
                continue
            yield ev
            emitted += 1
            if stop_after is not None and emitted >= stop_after:
                return


# ---------------------------------------------------------------------------
# Task 11 — reconnect / backoff math (no run-loop integration here; that
# orchestration lives in the gap_detector + Task 12 runner).
# ---------------------------------------------------------------------------

_BACKOFF_INITIAL: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_BACKOFF_CAP: float = 60.0
_BACKOFF_JITTER: float = 0.2  # ±20%
_UNHEALTHY_THRESHOLD: int = 3


def compute_backoff_delay(attempt: int) -> float:
    """Exponential backoff with ±20% jitter, capped at 60s.

    ``attempt`` is 0-indexed (attempt 0 → ~1s base, attempt 1 → ~2s, etc).
    Once the base reaches the 60s cap the jitter is applied to the cap.
    """
    base = min(_BACKOFF_INITIAL * (_BACKOFF_FACTOR**attempt), _BACKOFF_CAP)
    jitter = base * _BACKOFF_JITTER
    return base + random.uniform(-jitter, jitter)


def is_unhealthy(consecutive_failures: int) -> bool:
    """True once consecutive reconnect failures reach the unhealthy threshold (3)."""
    return consecutive_failures >= _UNHEALTHY_THRESHOLD
