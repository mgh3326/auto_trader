"""Sub-0.01 crypto prices keep their levels at the instrument's own precision.

Before this change every level was rounded to two decimals, so a KRW-XEC
frame near 0.007 collapsed to 0.0 / 0.01 and the supports/resistances split
came out empty on both sides. Crypto (Upbit KRW) now snaps to the Upbit order
tick; KR and US keep ``round(value, 2)``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze
from app.mcp_server.tooling.analysis_quick import _build_support_resistance
from app.mcp_server.tooling.fundamentals._support_resistance import (
    get_support_resistance_impl,
)
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _calculate_pivot,
)
from app.mcp_server.tooling.shared import round_price_for_market

XEC_LAST_CLOSE = 0.00706


def _xec_frame(n: int = 60) -> pd.DataFrame:
    """Prices oscillate around 0.007 on the 0.00001 tick; the last close is 0.00706."""
    idx = np.arange(n)
    close = np.round(0.00700 + 0.00040 * np.sin(idx / 4.0), 5)
    close[-1] = XEC_LAST_CLOSE
    high = np.round(close + 0.00010, 5)
    low = np.round(close - 0.00010, 5)
    volume = 1_000_000.0 + 50_000.0 * np.cos(idx)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-10-01", periods=n, freq="D").date,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


@pytest.mark.asyncio
async def test_xec_like_frame_keeps_tick_precision_and_both_sides() -> None:
    result = await get_support_resistance_impl(
        "KRW-XEC", market="crypto", preloaded_df=_xec_frame()
    )

    assert "error" not in result
    assert result["current_price"] == XEC_LAST_CLOSE
    assert result["current_price"] != 0.01

    supports = result["supports"]
    resistances = result["resistances"]
    assert supports, "no support levels for a sub-0.01 crypto frame"
    assert resistances, "no resistance levels for a sub-0.01 crypto frame"
    assert all(level["price"] < XEC_LAST_CLOSE for level in supports)
    assert all(level["price"] > XEC_LAST_CLOSE for level in resistances)


def test_quick_projection_levels_keep_tick_precision() -> None:
    supports, resistances = _build_support_resistance(
        _xec_frame(), XEC_LAST_CLOSE, "crypto"
    )

    assert supports
    assert resistances
    assert all(level["price"] < XEC_LAST_CLOSE for level in supports)
    assert all(level["price"] > XEC_LAST_CLOSE for level in resistances)


def test_pivot_levels_stay_distinct_for_sub_0_01_crypto() -> None:
    high = pd.Series([0.00710, 0.00710])
    low = pd.Series([0.00690, 0.00690])
    close = pd.Series([0.00700, 0.00700])

    pivot = _calculate_pivot(high, low, close, "crypto")

    assert pivot == {
        "p": 0.007,
        "r1": 0.0071,
        "r2": 0.0072,
        "r3": 0.0073,
        "s1": 0.0069,
        "s2": 0.0068,
        "s3": 0.0067,
    }
    assert len(set(pivot.values())) == 7


def test_fibonacci_levels_stay_distinct_for_sub_0_01_crypto() -> None:
    df = _xec_frame()
    df.loc[5, "low"] = 0.00600
    df.loc[20, "high"] = 0.00800

    fib = _calculate_fibonacci(df, XEC_LAST_CLOSE, "crypto")

    assert fib["levels"]["0.0"] == 0.008
    assert fib["levels"]["1.0"] == 0.006
    assert fib["levels"]["0.5"] == 0.007
    assert len(set(fib["levels"].values())) == 7


def test_round_price_for_market_snaps_crypto_to_upbit_tick() -> None:
    assert round_price_for_market(0.00706, "crypto") == XEC_LAST_CLOSE
    assert round_price_for_market(150_000_000.37, "crypto") == 150_000_000.0
    assert round_price_for_market(0.0, "crypto") == 0.0


@pytest.mark.parametrize("market", ["equity_kr", "equity_us", None])
def test_round_price_for_market_keeps_two_decimals_outside_crypto(
    market: str | None,
) -> None:
    for value in (72_345.678, 173_500.123, 12.345, 0.00706):
        assert round_price_for_market(value, market) == round(value, 2)


def test_intraday_distance_basis_price_uses_crypto_tick() -> None:
    sr = {
        "supports": [{"price": 0.0065, "strength": "weak", "sources": ["x"]}],
        "resistances": [],
    }
    analysis = {"support_resistance": sr, "quote": {"price": XEC_LAST_CLOSE}}

    analysis_analyze._recompute_intraday_support_resistance(analysis, "crypto")

    assert sr["distance_basis_price"] == XEC_LAST_CLOSE
    assert sr["distance_basis"] == "live_quote"
