"""ROB-993 — sizing helpers: quantize_qty + public exchangeInfo/price reads."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_strategy_loop.bars import build_bars_client
from app.services.brokers.binance.demo_strategy_loop.sizing import (
    fetch_reference_price,
    fetch_symbol_filters,
    quantize_qty,
)


def test_quantize_qty_uses_quantity_precision() -> None:
    result = quantize_qty(
        Decimal("30.00000000"), step_size=Decimal("0.1"), quantity_precision=1
    )
    assert result == Decimal("30.0")


def test_quantize_qty_falls_back_to_step_exponent() -> None:
    result = quantize_qty(
        Decimal("30.12"), step_size=Decimal("0.01"), quantity_precision=None
    )
    assert result == Decimal("30.12")


@pytest.mark.asyncio
async def test_fetch_symbol_filters_prefers_market_lot_size(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/exchangeInfo\?.*$"),
        json={
            "symbols": [
                {
                    "symbol": "XRPUSDT",
                    "quantityPrecision": 1,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        },
    )
    client = build_bars_client()
    try:
        filters = await fetch_symbol_filters(client, "XRPUSDT")
    finally:
        await client.aclose()
    assert filters["step_size"] == Decimal("0.1")
    assert filters["min_notional"] == Decimal("5")
    assert filters["quantity_precision"] == 1


@pytest.mark.asyncio
async def test_fetch_symbol_filters_missing_row_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/exchangeInfo\?.*$"),
        json={"symbols": [{"symbol": "BTCUSDT", "filters": []}]},
    )
    client = build_bars_client()
    try:
        with pytest.raises(RuntimeError, match="no row for"):
            await fetch_symbol_filters(client, "XRPUSDT")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_reference_price(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/ticker/price\?.*$"),
        json={"symbol": "XRPUSDT", "price": "0.55"},
    )
    client = build_bars_client()
    try:
        price = await fetch_reference_price(client, "XRPUSDT")
    finally:
        await client.aclose()
    assert price == Decimal("0.55")
