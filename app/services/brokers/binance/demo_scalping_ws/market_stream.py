"""ROB-317 — futures public market stream (read-only, fstream).

Parses the USD-M futures combined-stream payloads the daemon consumes:
aggTrade (momentum/freshness), bookTicker (spread/freshness), and closed
kline_1m (signal). Unsigned, read-only. Host is guarded against the
read-only PUBLIC_FUTURES_STREAM_HOSTS allowlist — never a signed mutation
host. See ROB-317 design §2, §4.

This module intentionally does NOT reuse BinancePublicWSClient's parser:
the futures payload shape differs (bookTicker carries "e":"bookTicker";
aggTrade is a stream the spot parser does not handle). It reuses only the
pure backoff helpers (compute_backoff_delay / is_unhealthy).
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
)
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent

_DEFAULT_BASE_URL = "wss://fstream.binance.com"


@dataclass(frozen=True, slots=True)
class AggTradeEvent:
    symbol: str
    price: Decimal
    qty: Decimal
    trade_time: dt.datetime
    is_buyer_maker: bool


FuturesWsEvent = KlineEvent | BookTickerEvent | AggTradeEvent


def parse_futures_message(raw: str, *, now: dt.datetime) -> FuturesWsEvent | None:
    """Parse one combined-stream message into a normalized event, or None.

    Returns None for malformed JSON, in-progress klines (``x: False``), and
    stream types the daemon does not consume. ``now`` is the receipt time
    used for bookTicker freshness (injected for deterministic tests).
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
    if not isinstance(data, dict):
        return None
    etype = data.get("e")
    if etype == "kline":
        k = data.get("k") or {}
        if not k.get("x"):
            return None
        return KlineEvent(
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
    if etype == "bookTicker":
        return BookTickerEvent(
            symbol=data["s"],
            bid_price=Decimal(data["b"]),
            bid_qty=Decimal(data["B"]),
            ask_price=Decimal(data["a"]),
            ask_qty=Decimal(data["A"]),
            received_at=now,
        )
    if etype == "aggTrade":
        return AggTradeEvent(
            symbol=data["s"],
            price=Decimal(data["p"]),
            qty=Decimal(data["q"]),
            trade_time=dt.datetime.fromtimestamp(data["T"] / 1000.0, tz=dt.UTC),
            is_buyer_maker=bool(data["m"]),
        )
    return None


def _assert_host_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    # Local test override mirrors ROB-285: 127.0.0.1 over plain ws is the
    # websockets.serve fixture; production is always wss to fstream.
    if host == "127.0.0.1" and parsed.scheme == "ws":
        return
    if host not in PUBLIC_FUTURES_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Futures stream host blocked: {host!r} not in "
            f"{sorted(PUBLIC_FUTURES_STREAM_HOSTS)}"
        )


def build_futures_stream_url(
    symbols: Sequence[str],
    *,
    streams: Sequence[str],
    base_url: str = _DEFAULT_BASE_URL,
) -> str:
    """Build a combined-stream URL for ``symbols`` × ``streams``, host-guarded."""
    _assert_host_allowed(base_url)
    parts = [f"{s.lower()}@{stream}" for s in symbols for stream in streams]
    return f"{base_url.rstrip('/')}/stream?streams=" + "/".join(parts)


class FuturesMarketStream:
    """Read-only futures combined-stream subscriber (host-guarded)."""

    def __init__(self, *, url: str) -> None:
        _assert_host_allowed(url)
        self._url = url
        self._ws: Any = None

    async def __aenter__(self) -> FuturesMarketStream:
        self._ws = await websockets.connect(self._url, ping_interval=20)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def events(
        self, *, stop_after: int | None = None
    ) -> AsyncIterator[FuturesWsEvent]:
        assert self._ws is not None, "FuturesMarketStream not connected"
        emitted = 0
        async for raw in self._ws:
            ev = parse_futures_message(raw, now=dt.datetime.now(tz=dt.UTC))
            if ev is None:
                continue
            yield ev
            emitted += 1
            if stop_after is not None and emitted >= stop_after:
                return
