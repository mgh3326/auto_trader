"""Tests for crypto composite score calculation."""

from __future__ import annotations

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_crypto_score import (
    BEARISH_NORMAL,
    BEARISH_STRONG,
    BULLISH,
    FLAT,
    HAMMER,
    _calculate_adx_di,
    calculate_20d_avg_volume,
    calculate_candle_coefficient,
    calculate_crypto_composite_score,
    calculate_crypto_metrics_from_ohlcv,
    calculate_rsi_score,
    calculate_trend_score,
    calculate_volume_score,
    extract_candle_values,
)
from app.mcp_server.tooling.market_data_indicators import _calculate_adx
from app.mcp_server.tooling.registry import register_all_tools
from app.services import upbit as upbit_service


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


class TestCandleCoefficient:
    def test_bullish_candle(self):
        coef, ctype = calculate_candle_coefficient(
            open_price=100.0, high=110.0, low=95.0, close=108.0
        )
        assert coef == 1.0
        assert ctype == BULLISH

    def test_bearish_strong_candle(self):
        coef, ctype = calculate_candle_coefficient(
            open_price=110.0, high=112.0, low=90.0, close=92.0
        )
        assert coef == 0.0
        assert ctype == BEARISH_STRONG

    def test_bullish_with_long_lower_shadow_prioritizes_bullish(self):
        open_price = 100.0
        close = 101.0
        low = 80.0
        high = 105.0
        body = abs(close - open_price)
        lower_shadow = min(open_price, close) - low
        assert lower_shadow > body * 2
        coef, ctype = calculate_candle_coefficient(
            open_price=open_price, high=high, low=low, close=close
        )
        assert coef == 1.0
        assert ctype == BULLISH

    def test_hammer_candle(self):
        coef, ctype = calculate_candle_coefficient(
            open_price=101.0, high=105.0, low=80.0, close=100.0
        )
        assert coef == 0.8
        assert ctype == HAMMER

    def test_bearish_normal_candle(self):
        coef, ctype = calculate_candle_coefficient(
            open_price=100.0, high=105.0, low=90.0, close=95.0
        )
        assert coef == 0.5
        assert ctype == BEARISH_NORMAL

    def test_flat_candle_zero_range(self):
        coef, ctype = calculate_candle_coefficient(
            open_price=100.0, high=100.0, low=100.0, close=100.0
        )
        assert coef == 0.5
        assert ctype == FLAT

    def test_none_values_return_flat(self):
        coef, ctype = calculate_candle_coefficient(None, 100.0, 90.0, 95.0)
        assert coef == 0.5
        assert ctype == FLAT

        coef, ctype = calculate_candle_coefficient(100.0, None, 90.0, 95.0)
        assert coef == 0.5
        assert ctype == FLAT


class TestVolumeScore:
    def test_normal_ratio(self):
        score = calculate_volume_score(2000.0, 1000.0)
        assert score == pytest.approx(66.6, rel=0.01)

    def test_high_ratio_capped_at_100(self):
        score = calculate_volume_score(10000.0, 1000.0)
        assert score == 100.0

    def test_none_today_volume(self):
        score = calculate_volume_score(None, 1000.0)
        assert score == 0.0

    def test_none_avg_volume(self):
        score = calculate_volume_score(1000.0, None)
        assert score == 0.0

    def test_zero_avg_volume(self):
        score = calculate_volume_score(1000.0, 0.0)
        assert score == 0.0


class TestTrendScore:
    def test_uptrend_plus_di_greater(self):
        score = calculate_trend_score(adx=25.0, plus_di=30.0, minus_di=20.0)
        assert score == 90.0

    def test_weak_trend_adx_below_35(self):
        score = calculate_trend_score(adx=25.0, plus_di=20.0, minus_di=30.0)
        assert score == 60.0

    def test_moderate_trend_adx_35_to_50(self):
        score = calculate_trend_score(adx=40.0, plus_di=20.0, minus_di=30.0)
        assert score == 30.0

    def test_strong_trend_adx_above_50(self):
        score = calculate_trend_score(adx=55.0, plus_di=20.0, minus_di=30.0)
        assert score == 10.0

    def test_none_adx_returns_conservative(self):
        score = calculate_trend_score(adx=None, plus_di=20.0, minus_di=30.0)
        assert score == 30.0

    def test_none_di_with_low_adx(self):
        score = calculate_trend_score(adx=25.0, plus_di=None, minus_di=None)
        assert score == 60.0


class TestRsiScore:
    def test_low_rsi_oversold(self):
        score = calculate_rsi_score(20.0)
        assert score == 80.0

    def test_high_rsi_overbought(self):
        score = calculate_rsi_score(80.0)
        assert score == 20.0

    def test_neutral_rsi(self):
        score = calculate_rsi_score(50.0)
        assert score == 50.0

    def test_none_rsi_returns_neutral(self):
        score = calculate_rsi_score(None)
        assert score == 50.0


class TestCompositeScore:
    def test_full_score_calculation(self):
        score = calculate_crypto_composite_score(
            rsi=30.0,
            volume_24h=2000.0,
            avg_volume_20d=1000.0,
            candle_coef=1.0,
            adx=30.0,
            plus_di=35.0,
            minus_di=20.0,
        )
        assert 0.0 <= score <= 100.0

    def test_score_clamped_at_100(self):
        score = calculate_crypto_composite_score(
            rsi=0.0,
            volume_24h=10000.0,
            avg_volume_20d=1000.0,
            candle_coef=1.0,
            adx=30.0,
            plus_di=35.0,
            minus_di=20.0,
        )
        assert score <= 100.0

    def test_score_clamped_at_0(self):
        score = calculate_crypto_composite_score(
            rsi=100.0,
            volume_24h=0.0,
            avg_volume_20d=1000.0,
            candle_coef=0.0,
            adx=60.0,
            plus_di=20.0,
            minus_di=30.0,
        )
        assert score >= 0.0

    def test_missing_rsi_uses_default(self):
        score = calculate_crypto_composite_score(
            rsi=None,
            volume_24h=1000.0,
            avg_volume_20d=1000.0,
            candle_coef=0.5,
            adx=30.0,
            plus_di=20.0,
            minus_di=30.0,
        )
        assert score > 0.0

    def test_missing_volume_uses_zero(self):
        score = calculate_crypto_composite_score(
            rsi=50.0,
            volume_24h=None,
            avg_volume_20d=1000.0,
            candle_coef=0.5,
            adx=30.0,
            plus_di=30.0,
            minus_di=20.0,
        )
        assert score > 0.0

    def test_missing_adx_uses_conservative(self):
        score = calculate_crypto_composite_score(
            rsi=50.0,
            volume_24h=1000.0,
            avg_volume_20d=1000.0,
            candle_coef=0.5,
            adx=None,
            plus_di=None,
            minus_di=None,
        )
        assert score > 0.0


class TestCalculate20dAvgVolume:
    def test_calculates_average(self):
        df = pd.DataFrame({"volume": [100.0] * 20})
        avg = calculate_20d_avg_volume(df)
        assert avg == 100.0

    def test_uses_last_20_days(self):
        df = pd.DataFrame({"volume": [50.0] * 10 + [100.0] * 20})
        avg = calculate_20d_avg_volume(df)
        assert avg == 100.0

    def test_returns_none_for_empty_df(self):
        df = pd.DataFrame()
        avg = calculate_20d_avg_volume(df)
        assert avg is None

    def test_returns_none_for_missing_column(self):
        df = pd.DataFrame({"close": [100.0] * 20})
        avg = calculate_20d_avg_volume(df)
        assert avg is None


class TestExtractCandleValues:
    def test_extracts_values(self):
        df = pd.DataFrame(
            {
                "open": [100.0, 105.0, 110.0],
                "high": [105.0, 110.0, 115.0],
                "low": [95.0, 100.0, 105.0],
                "close": [103.0, 108.0, 113.0],
            }
        )
        o, h, lo, c = extract_candle_values(df, -2)
        assert o == 105.0
        assert h == 110.0
        assert lo == 100.0
        assert c == 108.0

    def test_fallback_to_last_candle(self):
        df = pd.DataFrame(
            {
                "open": [100.0],
                "high": [105.0],
                "low": [95.0],
                "close": [103.0],
            }
        )
        o, h, lo, c = extract_candle_values(df, -1)
        assert o == 100.0
        assert h == 105.0

    def test_returns_none_for_missing_columns(self):
        df = pd.DataFrame({"close": [100.0] * 5})
        o, h, lo, c = extract_candle_values(df, -1)
        assert o is None

    def test_returns_none_for_empty_df(self):
        df = pd.DataFrame()
        o, h, lo, c = extract_candle_values(df, -1)
        assert all(v is None for v in (o, h, lo, c))


class TestAdxDiSharedUtilReuse:
    def test_calculates_adx_di(self):
        n = 30
        df = pd.DataFrame(
            {
                "high": [100.0 + i * 0.5 for i in range(n)],
                "low": [98.0 + i * 0.5 for i in range(n)],
                "close": [99.0 + i * 0.5 for i in range(n)],
            }
        )
        result = _calculate_adx_di(df)
        assert "adx" in result
        assert "plus_di" in result
        assert "minus_di" in result

    def test_returns_none_for_insufficient_data(self):
        df = pd.DataFrame(
            {
                "high": [100.0, 101.0],
                "low": [98.0, 99.0],
                "close": [99.0, 100.0],
            }
        )
        result = _calculate_adx_di(df)
        assert result["adx"] is None

    def test_matches_shared_adx_implementation(self):
        n = 50
        df = pd.DataFrame(
            {
                "high": [100.0 + i * 0.8 for i in range(n)],
                "low": [98.0 + i * 0.75 for i in range(n)],
                "close": [99.0 + i * 0.78 for i in range(n)],
            }
        )
        wrapped = _calculate_adx_di(df)
        shared = _calculate_adx(
            df["high"].astype(float),
            df["low"].astype(float),
            df["close"].astype(float),
        )
        assert wrapped == shared


class TestCalculateCryptoMetricsFromOhlcv:
    def test_returns_all_metrics(self):
        n = 50
        df = pd.DataFrame(
            {
                "open": [100.0 + i for i in range(n)],
                "high": [105.0 + i for i in range(n)],
                "low": [95.0 + i for i in range(n)],
                "close": [100.0 + i for i in range(n)],
                "volume": [1000.0 + i * 10 for i in range(n)],
            }
        )
        metrics = calculate_crypto_metrics_from_ohlcv(df)
        assert "rsi" in metrics
        assert "score" in metrics
        assert "volume_24h" in metrics
        assert "volume_ratio" in metrics
        assert "candle_type" in metrics
        assert "adx" in metrics
        assert metrics["volume_24h"] == 1000.0 + (n - 1) * 10

    def test_handles_empty_df(self):
        df = pd.DataFrame()
        metrics = calculate_crypto_metrics_from_ohlcv(df)
        assert metrics["rsi"] is None
        assert metrics["score"] is not None
        assert metrics["score"] >= 0.0


class TestScreenStocksCryptoScore:
    @pytest.fixture
    def mock_upbit_coins(self):
        return [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "trade_price": 100_000_000,
                "signed_change_rate": 0.01,
                "acc_trade_price_24h": 1_000_000_000_000,
                "acc_trade_volume_24h": 10_000,
            },
            {
                "market": "KRW-ETH",
                "korean_name": "이더리움",
                "trade_price": 5_000_000,
                "signed_change_rate": 0.02,
                "acc_trade_price_24h": 800_000_000_000,
                "acc_trade_volume_24h": 20_000,
            },
        ]

    @pytest.mark.asyncio
    async def test_crypto_screen_returns_score_field(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        import pandas as pd

        async def mock_fetch_ohlcv(symbol, market_type, count):
            return pd.DataFrame(
                {
                    "open": [100.0] * 50,
                    "high": [105.0] * 50,
                    "low": [95.0] * 50,
                    "close": [100.0 + i * 0.1 for i in range(50)],
                    "volume": [1000.0] * 50,
                }
            )

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        from app.mcp_server.tooling import analysis_screen_core

        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="asc",
            limit=5,
        )

        assert result["returned_count"] > 0
        for item in result["results"]:
            assert "score" in item
            assert item["score"] is not None
            assert 0.0 <= item["score"] <= 100.0


class TestRsiSortingNoneValues:
    @pytest.mark.asyncio
    async def test_rsi_none_sorts_to_end_asc(self, monkeypatch):
        from app.mcp_server.tooling import analysis_screen_core

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {"market": "KRW-A", "trade_price": 100, "acc_trade_price_24h": 1000},
                {"market": "KRW-B", "trade_price": 100, "acc_trade_price_24h": 1000},
                {"market": "KRW-C", "trade_price": 100, "acc_trade_price_24h": 1000},
            ]

        import pandas as pd

        async def mock_fetch_ohlcv(symbol, market_type, count):
            if symbol == "KRW-A":
                return pd.DataFrame({"close": [100.0 + i for i in range(50)]})
            elif symbol == "KRW-B":
                return pd.DataFrame({"close": [100.0 - i * 0.5 for i in range(50)]})
            else:
                return pd.DataFrame()

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="asc",
            limit=5,
        )

        rsi_values = [r.get("rsi") for r in result["results"]]
        none_indices = [i for i, v in enumerate(rsi_values) if v is None]
        if none_indices:
            assert none_indices[-1] == len(rsi_values) - 1

    @pytest.mark.asyncio
    async def test_rsi_none_sorts_to_end_desc(self, monkeypatch):
        from app.mcp_server.tooling import analysis_screen_core

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {"market": "KRW-A", "trade_price": 100, "acc_trade_price_24h": 1000},
                {"market": "KRW-B", "trade_price": 100, "acc_trade_price_24h": 1000},
                {"market": "KRW-C", "trade_price": 100, "acc_trade_price_24h": 1000},
            ]

        import pandas as pd

        async def mock_fetch_ohlcv(symbol, market_type, count):
            if symbol == "KRW-A":
                return pd.DataFrame({"close": [100.0 + i for i in range(50)]})
            elif symbol == "KRW-B":
                return pd.DataFrame({"close": [100.0 - i * 0.5 for i in range(50)]})
            else:
                return pd.DataFrame()

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=5,
        )

        rsi_values = [r.get("rsi") for r in result["results"]]
        none_indices = [i for i, v in enumerate(rsi_values) if v is None]
        if none_indices:
            assert none_indices[-1] == len(rsi_values) - 1


class TestRecommendStocksCryptoScore:
    @pytest.fixture
    def mock_upbit_coins(self):
        return [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "trade_price": 100_000_000,
                "signed_change_rate": 0.01,
                "acc_trade_price_24h": 1_000_000_000_000,
                "acc_trade_volume_24h": 10_000,
            },
            {
                "market": "KRW-ETH",
                "korean_name": "이더리움",
                "trade_price": 5_000_000,
                "signed_change_rate": 0.02,
                "acc_trade_price_24h": 800_000_000_000,
                "acc_trade_volume_24h": 20_000,
            },
        ]

    @pytest.mark.asyncio
    async def test_crypto_recommend_returns_numeric_score(
        self, mock_upbit_coins, monkeypatch
    ):
        from app.mcp_server.tooling import (
            analysis_screen_core,
            market_data_indicators,
            portfolio_holdings,
        )

        async def mock_fetch_top_traded_coins(fiat="KRW"):
            return mock_upbit_coins

        import pandas as pd

        async def mock_fetch_ohlcv(symbol, market_type, count):
            return pd.DataFrame(
                {
                    "open": [100.0] * 50,
                    "high": [105.0] * 50,
                    "low": [95.0] * 50,
                    "close": [100.0 + i * 0.1 for i in range(50)],
                    "volume": [1000.0] * 50,
                }
            )

        async def mock_collect_portfolio_positions(*args, **kwargs):
            return [], [], None, None

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        monkeypatch.setattr(
            market_data_indicators, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        monkeypatch.setattr(
            portfolio_holdings,
            "_collect_portfolio_positions",
            mock_collect_portfolio_positions,
        )

        tools = build_tools()
        result = await tools["recommend_stocks"](
            budget=10_000_000,
            market="crypto",
            strategy="balanced",
            max_positions=2,
        )

        assert result["recommendations"]
        for rec in result["recommendations"]:
            assert rec["score"] is not None
            assert isinstance(rec["score"], (int, float))
            assert 0.0 <= rec["score"] <= 100.0

    @pytest.mark.asyncio
    async def test_crypto_recommend_ohlcv_calls_limited_to_30(
        self, mock_upbit_coins, monkeypatch
    ):
        from app.mcp_server.tooling import (
            analysis_screen_core,
            market_data_indicators,
            portfolio_holdings,
        )

        async def mock_fetch_top_traded_coins(fiat="KRW"):
            return mock_upbit_coins

        ohlcv_call_count = 0

        import pandas as pd

        async def mock_fetch_ohlcv_counting(symbol, market_type, count):
            nonlocal ohlcv_call_count
            ohlcv_call_count += 1
            return pd.DataFrame(
                {
                    "open": [100.0] * 50,
                    "high": [105.0] * 50,
                    "low": [95.0] * 50,
                    "close": [100.0 + i * 0.1 for i in range(50)],
                    "volume": [1000.0] * 50,
                }
            )

        async def mock_collect_portfolio_positions(*args, **kwargs):
            return [], [], None, None

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv_counting,
        )
        monkeypatch.setattr(
            market_data_indicators,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv_counting,
        )
        monkeypatch.setattr(
            portfolio_holdings,
            "_collect_portfolio_positions",
            mock_collect_portfolio_positions,
        )

        tools = build_tools()
        await tools["recommend_stocks"](
            budget=10_000_000,
            market="crypto",
            strategy="balanced",
            max_positions=2,
        )

        assert ohlcv_call_count <= 30


class TestCryptoEnrichmentGracefulDegradation:
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_results_with_warning_message(
        self, monkeypatch
    ):
        from app.mcp_server.tooling import (
            analysis_screen_core,
            market_data_indicators,
            portfolio_holdings,
        )

        async def mock_fetch_top_traded_coins(fiat="KRW"):
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": 100_000_000,
                    "acc_trade_price_24h": 1_000_000_000_000,
                },
            ]

        import asyncio

        async def mock_fetch_ohlcv_slow(symbol, market_type, count):
            await asyncio.sleep(60)
            return pd.DataFrame()

        async def mock_collect_portfolio_positions(*args, **kwargs):
            return [], [], None, None

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv_slow
        )
        monkeypatch.setattr(
            market_data_indicators,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv_slow,
        )
        monkeypatch.setattr(
            portfolio_holdings,
            "_collect_portfolio_positions",
            mock_collect_portfolio_positions,
        )

        tools = build_tools()
        result = await tools["recommend_stocks"](
            budget=10_000_000,
            market="crypto",
            strategy="balanced",
            max_positions=1,
        )

        assert "error" not in result
        assert isinstance(result["warnings"], list)
        # Timeout path must degrade gracefully with explicit partial-results messaging.
        assert any(
            "timed out" in warning.lower() and "partial results" in warning.lower()
            for warning in result["warnings"]
        )
