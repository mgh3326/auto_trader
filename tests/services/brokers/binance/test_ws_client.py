"""ROB-285 — Binance public WS client (combined streams)."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app.services.brokers.binance.ws_client import (
    BinancePublicWSClient,
    KlineEvent,
)


@pytest.mark.asyncio
async def test_ws_yields_kline_events_for_closed_bars() -> None:
    """Wire a local websocket server, push one closed kline, assert
    the client yields a KlineEvent with is_closed=True."""
    received: list[KlineEvent] = []

    async def handler(ws):
        await ws.send(
            json.dumps(
                {
                    "stream": "btcusdt@kline_1m",
                    "data": {
                        "e": "kline",
                        "k": {
                            "t": 1700000000000,
                            "T": 1700000059999,
                            "s": "BTCUSDT",
                            "i": "1m",
                            "o": "30000.0",
                            "h": "30100.0",
                            "l": "29900.0",
                            "c": "30050.0",
                            "v": "12.5",
                            "q": "375625.0",
                            "n": 100,
                            "V": "6.0",
                            "Q": "180300.0",
                            "x": True,
                        },
                    },
                }
            )
        )
        # Keep connection open briefly for the client to drain.
        await asyncio.sleep(0.1)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    try:
        url = f"ws://127.0.0.1:{port}/stream?streams=btcusdt@kline_1m"
        async with BinancePublicWSClient(url=url) as ws:
            async for event in ws.events(stop_after=1):
                received.append(event)
                if len(received) >= 1:
                    break
    finally:
        server.close()
        await server.wait_closed()

    assert len(received) == 1
    ev = received[0]
    assert isinstance(ev, KlineEvent)
    assert ev.symbol == "BTCUSDT"
    assert ev.is_closed is True


@pytest.mark.asyncio
async def test_ws_drops_in_progress_klines() -> None:
    """ROB-285 §B.3 lock: in-progress klines (x=False) are dropped."""

    async def handler(ws):
        await ws.send(
            json.dumps(
                {
                    "stream": "btcusdt@kline_1m",
                    "data": {
                        "e": "kline",
                        "k": {
                            "t": 1700000000000,
                            "T": 1700000059999,
                            "s": "BTCUSDT",
                            "i": "1m",
                            "o": "30000",
                            "h": "30100",
                            "l": "29900",
                            "c": "30050",
                            "v": "12.5",
                            "q": "375625",
                            "n": 100,
                            "V": "6",
                            "Q": "180300",
                            "x": False,
                        },
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "stream": "btcusdt@kline_1m",
                    "data": {
                        "e": "kline",
                        "k": {
                            "t": 1700000060000,
                            "T": 1700000119999,
                            "s": "BTCUSDT",
                            "i": "1m",
                            "o": "30050",
                            "h": "30150",
                            "l": "29950",
                            "c": "30100",
                            "v": "8.0",
                            "q": "240800",
                            "n": 80,
                            "V": "4",
                            "Q": "120400",
                            "x": True,
                        },
                    },
                }
            )
        )
        await asyncio.sleep(0.1)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    received: list[KlineEvent] = []
    try:
        url = f"ws://127.0.0.1:{port}/stream?streams=btcusdt@kline_1m"
        async with BinancePublicWSClient(url=url) as ws:
            async for event in ws.events(stop_after=1):
                received.append(event)
                if len(received) >= 1:
                    break
    finally:
        server.close()
        await server.wait_closed()
    assert len(received) == 1
    assert isinstance(received[0], KlineEvent)
    assert received[0].is_closed is True  # In-progress was dropped.


@pytest.mark.asyncio
async def test_ws_rejects_non_allowed_host() -> None:
    from app.services.brokers.binance.errors import BinanceLiveHostBlocked

    with pytest.raises(BinanceLiveHostBlocked):
        BinancePublicWSClient(
            url="wss://evil.example.com/stream?streams=btcusdt@kline_1m"
        )
