"""ROB-443 Phase 1: crypto funding-rate enrichment (derivatives.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.invest_crypto_screener_snapshots.derivatives import (
    base_symbol_from_upbit,
    fetch_funding_rates,
    fetch_oi_and_long_short,
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


@pytest.mark.asyncio
async def test_fetch_oi_and_long_short_maps_perp_coins() -> None:
    async def _oi(base, period, limit):
        assert (period, limit) == ("1h", 24)
        return {
            "open_interest_history": [
                {"sum_open_interest_value_usd": 1.0},
                {"sum_open_interest_value_usd": 5_000_000.0},  # latest
            ],
            "oi_change_pct": 12.5,
        }

    async def _lsr(base, period, limit):
        return {"global_account": {"ratio": 1.8}}

    out = await fetch_oi_and_long_short(
        ["KRW-BTC", "KRW-XYZ"], oi_fetcher=_oi, lsr_fetcher=_lsr
    )
    assert out["KRW-BTC"]["open_interest_usd"] == Decimal("5000000.0")
    assert out["KRW-BTC"]["oi_change_24h"] == Decimal("12.5")
    assert out["KRW-BTC"]["long_short_account_ratio"] == Decimal("1.8")


@pytest.mark.asyncio
async def test_fetch_oi_and_long_short_fail_open_per_metric() -> None:
    async def _oi_boom(base, period, limit):
        raise RuntimeError("oi down")

    async def _lsr(base, period, limit):
        return {"global_account": {"ratio": 0.7}}

    out = await fetch_oi_and_long_short(
        ["KRW-BTC"], oi_fetcher=_oi_boom, lsr_fetcher=_lsr
    )
    # OI failed but long/short still resolved → coin kept with partial data
    assert out["KRW-BTC"].get("open_interest_usd") is None
    assert out["KRW-BTC"]["long_short_account_ratio"] == Decimal("0.7")


@pytest.mark.asyncio
async def test_fetch_oi_and_long_short_empty_when_no_perp_symbols() -> None:
    called = False

    async def _oi(base, period, limit):
        nonlocal called
        called = True
        return {}

    out = await fetch_oi_and_long_short(["BTC-ETH"], oi_fetcher=_oi, lsr_fetcher=_oi)
    assert out == {} and called is False
