"""ROB-443 Phase 1: crypto funding-rate enrichment (derivatives.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.invest_crypto_screener_snapshots.derivatives import (
    base_symbol_from_upbit,
    fetch_funding_rates,
)


@pytest.mark.parametrize(
    "upbit,expected",
    [
        ("KRW-BTC", "BTC"),
        ("krw-eth", "ETH"),
        ("BTC-ETH", None),  # not a KRW market
        ("USDT-BTC", None),
        ("KRW-", None),
        ("", None),
    ],
)
def test_base_symbol_from_upbit(upbit, expected) -> None:
    assert base_symbol_from_upbit(upbit) == expected


@pytest.mark.asyncio
async def test_fetch_funding_rates_maps_perp_coins_and_skips_no_perp() -> None:
    async def _fetcher(symbols):
        # batch returns base-symbol rows only for coins that have a Binance perp
        assert symbols == [
            "BTC",
            "ETH",
            "XYZ",
        ]  # all bases requested; XYZ absent in result
        return [
            {"symbol": "BTC", "funding_rate": 0.0001},
            {"symbol": "ETH", "funding_rate": -0.00025},
            # XYZ (Upbit-only) absent → no perp
        ]

    out = await fetch_funding_rates(["KRW-BTC", "KRW-ETH", "KRW-XYZ"], fetcher=_fetcher)
    assert out == {"KRW-BTC": Decimal("0.0001"), "KRW-ETH": Decimal("-0.00025")}
    assert "KRW-XYZ" not in out  # no perp → omitted (caller treats as None)


@pytest.mark.asyncio
async def test_fetch_funding_rates_fail_open_on_error() -> None:
    async def _boom(symbols):
        raise RuntimeError("binance down")

    # fail-open: build proceeds without funding enrichment
    assert await fetch_funding_rates(["KRW-BTC"], fetcher=_boom) == {}


@pytest.mark.asyncio
async def test_fetch_funding_rates_empty_when_no_krw_symbols() -> None:
    called = False

    async def _fetcher(symbols):
        nonlocal called
        called = True
        return []

    out = await fetch_funding_rates(["BTC-ETH", "USDT-XRP"], fetcher=_fetcher)
    assert out == {}
    assert called is False  # no KRW coins → no fetch attempted
