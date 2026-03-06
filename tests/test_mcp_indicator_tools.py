"""
Tests for MCP indicator tools.

This module tests get_indicators, fetch helpers, RSI map, volume profile,
and support/resistance indicator functionality.
"""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import (
    market_data_indicators,
    portfolio_holdings,
)
from tests._mcp_tooling_support import (
    DummyMCP,
    build_tools,
    _patch_runtime_attr,
    _single_row_df,
)


@pytest.mark.asyncio
async def test_get_indicators_supports_new_indicators(monkeypatch):
    tools = build_tools()
    rows = 80
    close = pd.Series([100.0 + i * 0.2 + np.sin(i) for i in range(rows)])
    df = pd.DataFrame(
        {
            "close": close,
            "high": close + 1.5,
            "low": close - 1.5,
            "volume": pd.Series([1000.0 + i * 10 for i in range(rows)]),
        }
    )

    _patch_runtime_attr(
        monkeypatch, "_fetch_ohlcv_for_indicators", AsyncMock(return_value=df)
    )

    result = await tools["get_indicators"](
        "KRW-BTC", indicators=["adx", "stoch_rsi", "obv"]
    )

    assert "error" not in result
    assert "indicators" in result
    assert "adx" in result["indicators"]
    assert "stoch_rsi" in result["indicators"]
    assert "obv" in result["indicators"]
    assert set(result["indicators"]["adx"].keys()) == {"adx", "plus_di", "minus_di"}
    assert set(result["indicators"]["stoch_rsi"].keys()) == {"k", "d"}
    assert set(result["indicators"]["obv"].keys()) == {"obv", "signal", "divergence"}


@pytest.mark.asyncio
async def test_get_indicators_plain_alpha_symbol_requires_market(monkeypatch):
    tools = build_tools()
    fetch_mock = AsyncMock()
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    with pytest.raises(ValueError) as exc_info:
        await tools["get_indicators"]("ETC", indicators=["rsi"])

    assert str(exc_info.value) == (
        "market is required for plain alphabetic symbols. Use market='us' "
        "for US equities, or provide KRW-/USDT- prefixed symbol for crypto."
    )
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_indicators_plain_alpha_symbol_rejects_empty_market(monkeypatch):
    tools = build_tools()
    fetch_mock = AsyncMock()
    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fetch_mock)

    with pytest.raises(ValueError) as exc_info:
        await tools["get_indicators"]("ETC", indicators=["rsi"], market="")

    assert str(exc_info.value) == (
        "market is required for plain alphabetic symbols. Use market='us' "
        "for US equities, or provide KRW-/USDT- prefixed symbol for crypto."
    )
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_indicators_rejects_invalid_indicator_with_new_valid_options():
    tools = build_tools()

    with pytest.raises(ValueError, match="Invalid indicator") as exc_info:
        await tools["get_indicators"]("KRW-BTC", indicators=["not_a_real_indicator"])

    message = str(exc_info.value)
    assert "Valid options" in message
    assert "adx" in message
    assert "stoch_rsi" in message
    assert "obv" in message


@pytest.mark.asyncio
async def test_get_indicators_obv_returns_error_when_volume_column_missing(monkeypatch):
    tools = build_tools()
    rows = 40
    close = pd.Series([100.0 + i * 0.1 for i in range(rows)])
    df_no_volume = pd.DataFrame(
        {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df_no_volume),
    )

    result = await tools["get_indicators"]("AAPL", indicators=["obv"], market="us")

    assert result["source"] == "yahoo"
    assert "error" in result
    assert "Missing required columns" in result["error"]
    assert "volume" in result["error"]


@pytest.mark.asyncio
async def test_get_indicators_crypto_uses_ticker_price(monkeypatch):
    tools = build_tools()
    rows = 40
    close = pd.Series([100.0 + i for i in range(rows)])
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": pd.Series([1000.0 + i for i in range(rows)]),
            "value": pd.Series([1000000.0 + i for i in range(rows)]),
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    ticker_mock = AsyncMock(return_value={"KRW-BTC": 123456789.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", ticker_mock)

    result = await tools["get_indicators"]("KRW-BTC", indicators=["rsi"])

    assert "error" not in result
    assert result["price"] == 123456789.0
    ticker_mock.assert_awaited_once_with(["KRW-BTC"])


@pytest.mark.asyncio
async def test_get_indicators_crypto_rsi_uses_existing_ohlcv_and_ticker(monkeypatch):
    tools = build_tools()
    rows = 40
    close = pd.Series([100.0 + i for i in range(rows)])
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": pd.Series([1000.0 + i for i in range(rows)]),
            "value": pd.Series([1000000.0 + i for i in range(rows)]),
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 50.0}),
    )

    result = await tools["get_indicators"]("KRW-BTC", indicators=["rsi"])
    expected_rsi = market_data_indicators._compute_crypto_realtime_rsi_from_frame(
        df,
        50.0,
    )

    assert "error" not in result
    assert result["indicators"]["rsi"]["14"] == expected_rsi


@pytest.mark.asyncio
async def test_get_indicators_crypto_ticker_failure_falls_back_to_close(monkeypatch):
    tools = build_tools()
    rows = 40
    close = pd.Series([100.0 + i for i in range(rows)])
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": pd.Series([1000.0 + i for i in range(rows)]),
            "value": pd.Series([1000000.0 + i for i in range(rows)]),
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    ticker_mock = AsyncMock(side_effect=RuntimeError("ticker unavailable"))
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", ticker_mock)

    result = await tools["get_indicators"]("KRW-BTC", indicators=["rsi"])

    assert "error" not in result
    assert result["price"] == float(df["close"].iloc[-1])
    ticker_mock.assert_awaited_once_with(["KRW-BTC"])


@pytest.mark.asyncio
async def test_portfolio_indicators_crypto_rsi_uses_existing_ohlcv_and_ticker(
    monkeypatch,
):
    rows = 40
    close = pd.Series([100.0 + i for i in range(rows)])
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": pd.Series([1000.0 + i for i in range(rows)]),
            "value": pd.Series([1000000.0 + i for i in range(rows)]),
        }
    )

    monkeypatch.setattr(
        portfolio_holdings,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 65.0}),
    )

    result = await portfolio_holdings._get_indicators_impl(
        "KRW-BTC", indicators=["rsi"], market="crypto"
    )
    expected_rsi = market_data_indicators._compute_crypto_realtime_rsi_from_frame(
        df,
        65.0,
    )

    assert "error" not in result
    assert result["indicators"]["rsi"]["14"] == expected_rsi


@pytest.mark.asyncio
async def test_portfolio_indicators_crypto_ticker_failure_falls_back_to_close(
    monkeypatch,
):
    rows = 40
    close = pd.Series([100.0 + i for i in range(rows)])
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=rows, freq="D").date,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": pd.Series([1000.0 + i for i in range(rows)]),
            "value": pd.Series([1000000.0 + i for i in range(rows)]),
        }
    )

    monkeypatch.setattr(
        portfolio_holdings,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    ticker_mock = AsyncMock(side_effect=RuntimeError("ticker unavailable"))
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", ticker_mock)

    result = await portfolio_holdings._get_indicators_impl(
        "KRW-BTC", indicators=["rsi"], market="crypto"
    )

    assert "error" not in result
    assert result["price"] == float(df["close"].iloc[-1])
    ticker_mock.assert_awaited_once_with(["KRW-BTC"])


@pytest.mark.asyncio
async def test_fetch_ohlcv_for_indicators_crypto_uses_upbit_service_boundary(
    monkeypatch,
):
    service_df = pd.DataFrame(
        {
            "date": [date(2026, 2, 13), date(2026, 2, 14)],
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "volume": [1000.0, 1001.0],
            "value": [100000.0, 100100.0],
        }
    )
    service_mock = AsyncMock(return_value=service_df)

    monkeypatch.setattr(upbit_service, "fetch_ohlcv", service_mock)

    result = await market_data_indicators._fetch_ohlcv_for_indicators(
        "KRW-BTC", "crypto", count=2
    )

    assert len(result) == 2
    service_mock.assert_awaited_once_with(
        market="KRW-BTC",
        days=2,
        period="day",
        end_date=None,
    )


@pytest.mark.asyncio
async def test_fetch_ohlcv_for_indicators_kr_uses_un_market(monkeypatch):
    service_df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            called["period"] = period
            return service_df

    monkeypatch.setattr(market_data_indicators, "KISClient", DummyKISClient)

    result = await market_data_indicators._fetch_ohlcv_for_indicators(
        "005930", "equity_kr", count=300
    )

    assert len(result) == 1
    assert called["code"] == "005930"
    assert called["market"] == "UN"
    assert called["n"] == 250
    assert called["period"] == "D"


@pytest.mark.asyncio
async def test_fetch_ohlcv_for_volume_profile_kr_uses_un_market(monkeypatch):
    service_df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            called["period"] = period
            return service_df

    monkeypatch.setattr(market_data_indicators, "KISClient", DummyKISClient)

    result = await market_data_indicators._fetch_ohlcv_for_volume_profile(
        "005930", "equity_kr", period_days=60
    )

    assert len(result) == 1
    assert called["code"] == "005930"
    assert called["market"] == "UN"
    assert called["n"] == 60
    assert called["period"] == "D"


@pytest.mark.asyncio
async def test_compute_crypto_realtime_rsi_map_uses_single_batch_ticker_call(
    monkeypatch,
):
    btc_close = pd.Series([100.0 + i for i in range(40)])
    eth_close = pd.Series([200.0 + i for i in range(40)])
    dfs = {
        "KRW-BTC": pd.DataFrame({"close": btc_close}),
        "KRW-ETH": pd.DataFrame({"close": eth_close}),
    }

    async def fake_fetch_ohlcv(symbol: str, market_type: str, count: int = 250):
        assert market_type == "crypto"
        assert count == 200
        return dfs[symbol]

    monkeypatch.setattr(
        market_data_indicators,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(side_effect=fake_fetch_ohlcv),
    )
    ticker_mock = AsyncMock(return_value={"KRW-BTC": 150.0, "KRW-ETH": 260.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", ticker_mock)

    result = await market_data_indicators.compute_crypto_realtime_rsi_map(
        ["KRW-BTC", "KRW-ETH"]
    )

    assert set(result.keys()) == {"KRW-BTC", "KRW-ETH"}
    ticker_mock.assert_awaited_once_with(["KRW-BTC", "KRW-ETH"], use_cache=True)


@pytest.mark.asyncio
async def test_compute_crypto_realtime_rsi_map_uses_ticker_override_on_last_close(
    monkeypatch,
):
    close = pd.Series([100.0 + i for i in range(40)])
    df = pd.DataFrame({"close": close})

    monkeypatch.setattr(
        market_data_indicators,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 50.0}),
    )

    result = await market_data_indicators.compute_crypto_realtime_rsi_map(["KRW-BTC"])

    expected_close = df["close"].copy()
    expected_close.iloc[-1] = 50.0
    expected_rsi = market_data_indicators._calculate_rsi(expected_close).get("14")
    assert result["KRW-BTC"] == expected_rsi


@pytest.mark.asyncio
async def test_compute_crypto_realtime_rsi_map_allows_under_200_but_min_15(monkeypatch):
    close = pd.Series(
        [
            50.0,
            51.0,
            52.0,
            51.0,
            53.0,
            52.0,
            54.0,
            53.0,
            55.0,
            54.0,
            56.0,
            55.0,
            57.0,
            56.0,
            58.0,
        ]
    )
    df = pd.DataFrame({"close": close})

    monkeypatch.setattr(
        market_data_indicators,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={}),
    )

    result = await market_data_indicators.compute_crypto_realtime_rsi_map(["KRW-NEW"])
    assert result["KRW-NEW"] is not None


@pytest.mark.asyncio
async def test_compute_crypto_realtime_rsi_map_returns_none_when_less_than_15_valid(
    monkeypatch,
):
    close = pd.Series([np.nan] * 6 + [100.0 + i for i in range(14)])
    df = pd.DataFrame({"close": close})

    monkeypatch.setattr(
        market_data_indicators,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-NEW": 200.0}),
    )

    result = await market_data_indicators.compute_crypto_realtime_rsi_map(["KRW-NEW"])
    assert result["KRW-NEW"] is None


@pytest.mark.unit
def test_calculate_volume_profile_distributes_volume_proportionally():
    df = pd.DataFrame(
        [
            {
                "low": 0.0,
                "high": 10.0,
                "volume": 100.0,
            }
        ]
    )

    result = market_data_indicators._calculate_volume_profile(
        df, bins=2, value_area_ratio=0.70
    )

    assert result["price_range"] == {"low": 0, "high": 10}
    assert result["poc"]["volume"] == 50
    assert result["profile"][0]["volume"] == 50
    assert result["profile"][1]["volume"] == 50
    assert result["profile"][0]["volume_pct"] == 50
    assert result["profile"][1]["volume_pct"] == 50


@pytest.mark.unit
def test_calculate_volume_profile_ignores_rows_with_nan_values():
    df = pd.DataFrame(
        [
            {"low": 0.0, "high": 10.0, "volume": 100.0},
            {"low": np.nan, "high": 12.0, "volume": 40.0},
            {"low": 1.0, "high": np.nan, "volume": 40.0},
            {"low": 2.0, "high": 8.0, "volume": np.nan},
        ]
    )

    result = market_data_indicators._calculate_volume_profile(
        df, bins=2, value_area_ratio=0.70
    )

    assert result["price_range"] == {"low": 0, "high": 10}
    assert sum(level["volume"] for level in result["profile"]) == 100
    assert result["poc"]["volume"] == 50


@pytest.mark.asyncio
async def test_get_support_resistance_clusters_levels(monkeypatch):
    tools = build_tools()

    base_df = pd.DataFrame(
        [
            {
                "date": "2026-02-01",
                "high": 120.0,
                "low": 80.0,
                "close": 100.0,
                "volume": 1000,
            }
        ]
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=base_df[["date", "high", "low", "close"]]),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_volume_profile",
        AsyncMock(return_value=base_df),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_calculate_fibonacci",
        lambda df, current_price: {
            "swing_high": {"price": 120.0, "date": "2026-02-01"},
            "swing_low": {"price": 80.0, "date": "2026-01-01"},
            "trend": "retracement_from_high",
            "current_price": 100.0,
            "levels": {"0.382": 110.0, "0.618": 95.0, "0.786": 89.0},
            "nearest_support": {"level": "0.618", "price": 95.0},
            "nearest_resistance": {"level": "0.382", "price": 110.0},
        },
    )
    _patch_runtime_attr(
        monkeypatch,
        "_calculate_volume_profile",
        lambda df, bins, value_area_ratio=0.70: {
            "price_range": {"low": 80.0, "high": 120.0},
            "poc": {"price": 90.0, "volume": 5000.0},
            "value_area": {"high": 111.0, "low": 89.0, "volume_pct": 70.0},
            "profile": [],
        },
    )
    _patch_runtime_attr(
        monkeypatch,
        "_compute_indicators",
        lambda df, indicators: {
            "bollinger": {"upper": 111.0, "middle": 100.0, "lower": 90.0}
        },
    )

    result = await tools["get_support_resistance"]("KRW-BTC")

    assert result["symbol"] == "KRW-BTC"
    assert result["current_price"] == 100.0
    assert result["supports"]
    assert result["resistances"]

    strong_supports = [s for s in result["supports"] if s["strength"] == "strong"]
    strong_resistances = [r for r in result["resistances"] if r["strength"] == "strong"]
    assert strong_supports
    assert strong_resistances
    assert "volume_poc" in strong_supports[0]["sources"]

    # Verify distance_pct is present and correctly calculated
    for s in result["supports"]:
        assert "distance_pct" in s
        expected = round((s["price"] - 100.0) / 100.0 * 100, 2)
        assert s["distance_pct"] == expected
        assert s["distance_pct"] < 0  # supports are below current price
    for r in result["resistances"]:
        assert "distance_pct" in r
        expected = round((r["price"] - 100.0) / 100.0 * 100, 2)
        assert r["distance_pct"] == expected
        assert r["distance_pct"] > 0  # resistances are above current price


@pytest.mark.asyncio
async def test_get_support_resistance_uses_single_ohlcv_fetch(monkeypatch):
    """Verify get_support_resistance fetches OHLCV only once (not twice)."""
    from app.mcp_server.tooling import fundamentals_handlers

    tools = build_tools()
    fetch_calls = []

    async def mock_fetch_ohlcv(symbol, market_type, count):
        fetch_calls.append((symbol, market_type, count))
        return pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=60, freq="D"),
                "open": [100.0] * 60,
                "high": [105.0] * 60,
                "low": [95.0] * 60,
                "close": [102.0] * 60,
                "volume": [1000] * 60,
            }
        )

    # Patch in fundamentals_handlers since that's where the import happens
    monkeypatch.setattr(
        fundamentals_handlers,
        "_fetch_ohlcv_for_indicators",
        mock_fetch_ohlcv,
    )

    result = await tools["get_support_resistance"]("AAPL", market="us")

    # Should only fetch once, not twice
    assert len(fetch_calls) == 1
    assert fetch_calls[0][0] == "AAPL"
    assert result["symbol"] == "AAPL"
