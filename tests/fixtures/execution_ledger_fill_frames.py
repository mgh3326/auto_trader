"""Real-shaped broker fill frames shared by the fillwire P0 tests.

These are the exact frame shapes ``tests/test_websocket_monitor.py`` already
drives ``_on_upbit_order`` / ``_on_kis_execution`` with, lifted into one place
so the ingest, sink and downstream tests all normalize the *same* payloads the
live tap sees. Values are synthetic: no account, key, token, or real event
provenance is recorded here.
"""

from __future__ import annotations

from typing import Any

#: Upbit ``myOrder`` partial-fill frame (``state="trade"``).
UPBIT_TRADE_FRAME: dict[str, Any] = {
    "code": "KRW-BTC",
    "uuid": "upbit-order-fillwire-1",
    "identifier": "fillwire-proposal-rung",
    "ask_bid": "BID",
    "trade_price": 92_800_000,
    "trade_volume": 0.0003,
    "executed_volume": "0.0003",
    "state": "trade",
    "trade_uuid": "upbit-trade-fillwire-1",
    "trade_timestamp": 1_752_409_595_000,
}

#: KIS domestic execution frame (``fill_yn="2"`` = 체결).
KIS_DOMESTIC_FILL_FRAME: dict[str, Any] = {
    "symbol": "000660",
    "side": "sell",
    "fill_yn": "2",
    "filled_price": 1_959_000,
    "filled_qty": 1,
    "market": "kr",
    "correlation_id": "corr-fillwire-kis-1",
    "order_id": "0006421200",
    "fill_seq": "7",
}

#: KIS overseas execution frame (USD currency path).
KIS_OVERSEAS_FILL_FRAME: dict[str, Any] = {
    "symbol": "AAPL",
    "side": "buy",
    "fill_yn": "2",
    "filled_price": 212.5,
    "filled_qty": 3,
    "market": "us",
    "currency": "USD",
    "correlation_id": "corr-fillwire-us-1",
    "order_id": "0006421201",
    "ovrs_excg_cd": "NASD",
}


def upbit_trade_frame(**overrides: Any) -> dict[str, Any]:
    return {**UPBIT_TRADE_FRAME, **overrides}


def kis_domestic_fill_frame(**overrides: Any) -> dict[str, Any]:
    return {**KIS_DOMESTIC_FILL_FRAME, **overrides}


def kis_overseas_fill_frame(**overrides: Any) -> dict[str, Any]:
    return {**KIS_OVERSEAS_FILL_FRAME, **overrides}
