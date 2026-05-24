"""ROB-307 PR2 — tests for the read-only demo-host reference fetcher.

Fetches the MARKET sizing step, MIN_NOTIONAL, and a reference price from
exchangeInfo + ticker/price on Demo hosts only. Network is mocked; no
signing, no credentials.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping_exec.reference import (
    DemoReferenceData,
    ReferenceDataError,
    SymbolReference,
)

_SPOT_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "XRPUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.10000000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ],
        }
    ]
}

_FUTURES_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "XRPUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.1"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
    ]
}


@pytest.mark.asyncio
async def test_fetch_spot_reference(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/exchangeInfo\?.*$"),
        json=_SPOT_EXCHANGE_INFO,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/ticker/price\?.*$"),
        json={"symbol": "XRPUSDT", "price": "1.36010000"},
    )
    ref = await DemoReferenceData().fetch("spot", "XRPUSDT")
    assert ref == SymbolReference(
        price=Decimal("1.36010000"),
        step_size=Decimal("0.10000000"),
        min_notional=Decimal("5.00000000"),
    )


@pytest.mark.asyncio
async def test_fetch_futures_reference_uses_demo_fapi_and_market_lot(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/exchangeInfo\?.*$"),
        json=_FUTURES_EXCHANGE_INFO,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-fapi\.binance\.com/fapi/v1/ticker/price\?.*$"),
        json={"symbol": "XRPUSDT", "price": "1.3595"},
    )
    ref = await DemoReferenceData().fetch("usdm_futures", "XRPUSDT")
    assert ref.price == Decimal("1.3595")
    assert ref.step_size == Decimal("0.1")
    assert ref.min_notional == Decimal("5")


@pytest.mark.asyncio
async def test_missing_symbol_row_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"^https://demo-api\.binance\.com/api/v3/exchangeInfo\?.*$"),
        json={"symbols": []},
    )
    with pytest.raises(ReferenceDataError):
        await DemoReferenceData().fetch("spot", "XRPUSDT")
