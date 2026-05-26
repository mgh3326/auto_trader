"""ROB-317 — FuturesMarketStream round-trip over a local websockets server."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import websockets

from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    FuturesMarketStream,
)
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.ws_client import KlineEvent

pytestmark = pytest.mark.asyncio


async def test_stream_yields_parsed_events_from_local_server() -> None:
    messages = [
        json.dumps(
            {
                "stream": "xrpusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "s": "XRPUSDT",
                    "p": "0.51",
                    "q": "1",
                    "T": 1716724800000,
                    "m": False,
                },
            }
        ),
        json.dumps(
            {
                "stream": "xrpusdt@kline_1m",
                "data": {
                    "e": "kline",
                    "s": "XRPUSDT",
                    "k": {
                        "t": 1716724740000,
                        "T": 1716724799999,
                        "s": "XRPUSDT",
                        "i": "1m",
                        "o": "0.50",
                        "h": "0.52",
                        "l": "0.49",
                        "c": "0.515",
                        "v": "1000",
                        "q": "515",
                        "n": 42,
                        "x": True,
                    },
                },
            }
        ),
    ]

    async def handler(ws) -> None:
        for m in messages:
            await ws.send(m)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/stream?streams=xrpusdt@aggTrade"
        out = []
        async with FuturesMarketStream(url=url) as stream:
            async for ev in stream.events(stop_after=2):
                out.append(ev)

    assert isinstance(out[0], AggTradeEvent)
    assert out[0].price == Decimal("0.51")
    assert isinstance(out[1], KlineEvent)
    assert out[1].close == Decimal("0.515")


async def test_stream_rejects_non_fstream_host() -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        FuturesMarketStream(
            url="wss://fapi.binance.com/stream?streams=xrpusdt@aggTrade"
        )
