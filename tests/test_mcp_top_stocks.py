from collections.abc import Callable
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yfinance as yf

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import analysis_screening, analysis_tool_handlers
from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, Callable[..., Any]]:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp))
    return mcp.tools


@pytest.mark.asyncio
class TestMCPTopStocks:
    @pytest.fixture(autouse=True)
    def _neutralize_foreigners_market_cap_fetch(self, monkeypatch):
        # ROB-629 B2: the foreigners backfill block calls a DB reader
        # (_fetch_market_cap_maps). Keep these routing/mapping tests hermetic and
        # fast — only TestForeignersLiquidity exercises real caps.
        async def _no_op_fetch(*args, **kwargs):
            return {}, {}

        monkeypatch.setattr(
            "app.mcp_server.tooling.foreigners_liquidity._fetch_market_cap_maps",
            _no_op_fetch,
        )

    async def test_get_top_stocks_us_uses_analysis_screening_rankings_alias(
        self, monkeypatch
    ):
        tools = build_tools()

        async def fake_get_us_rankings(ranking_type: str, limit: int):
            assert ranking_type == "volume"
            assert limit == 3
            return ([{"rank": 1, "symbol": "AAPL", "name": "Apple"}], "shim-us")

        monkeypatch.setattr(
            analysis_screening, "_get_us_rankings", fake_get_us_rankings
        )

        result = await tools["get_top_stocks"](
            market="us", ranking_type="volume", limit=3
        )

        assert result["source"] == "shim-us"
        assert result["rankings"][0]["symbol"] == "AAPL"

    async def test_kr_volume_rank(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "2.5",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "1.5",
                        "acml_vol": "5000000",
                        "hts_avls": "50000000000000",
                        "acml_tr_pbmn": "600000000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="volume")

        assert result["market"] == "kr"
        assert result["ranking_type"] == "volume"
        assert result["total_count"] == 2
        assert len(result["rankings"]) == 2
        assert result["rankings"][0]["rank"] == 1
        assert result["rankings"][0]["symbol"] == "005930"
        assert result["rankings"][0]["name"] == "삼성전자"
        assert result["rankings"][0]["change_rate"] == pytest.approx(2.5)
        assert result["source"] == "kis"

    async def test_kr_volume_rank_fallback_to_mksc_shrn_iscd(self, monkeypatch):
        """KR 응답에 stck_shrn_iscd가 없고 mksc_shrn_iscd만 있는 경우 fallback 동작 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.2",
                        "acml_vol": "20000000",
                        "hts_avls": "5000000000000",
                        "acml_tr_pbmn": "700000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="volume")

        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["rank"] == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"
        assert result["source"] == "kis"

    async def test_kr_volume_rank_mixed_symbol_fields(self, monkeypatch):
        """응답에 stck_shrn_iscd와 mksc_shrn_iscd가 혼합된 경우 우선순위 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "mksc_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "2.5",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    },
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.2",
                        "acml_vol": "20000000",
                        "hts_avls": "5000000000000",
                        "acml_tr_pbmn": "700000000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="volume")

        # stck_shrn_iscd가 있는 경우 우선 사용
        assert result["rankings"][0]["symbol"] == "005930"
        assert result["rankings"][0]["name"] == "삼성전자"
        # mksc_shrn_iscd만 있는 경우 fallback 사용
        assert result["rankings"][1]["symbol"] == "900210"
        assert result["rankings"][1]["name"] == "KODEX 200"

    async def test_kr_gainers_ranking_fallback_to_mksc_shrn_iscd(self, monkeypatch):
        """gainers 랭킹에서 mksc_shrn_iscd fallback 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                if direction == "up":
                    return [
                        {
                            "mksc_shrn_iscd": "900210",
                            "hts_kor_isnm": "KODEX 200",
                            "stck_prpr": "35000",
                            "prdy_ctrt": "5.0",
                            "acml_vol": "20000000",
                            "hts_avls": "5000000000000",
                            "acml_tr_pbmn": "700000000000000",
                        }
                    ]
                return []

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="gainers")

        assert result["ranking_type"] == "gainers"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"
        assert result["rankings"][0]["change_rate"] == pytest.approx(5.0)

    async def test_kr_market_cap_ranking_fallback_to_mksc_shrn_iscd(self, monkeypatch):
        """market_cap 랭킹에서 mksc_shrn_iscd fallback 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def market_cap_rank(self, market, limit):
                return [
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.2",
                        "acml_vol": "20000000",
                        "hts_avls": "5000000000000",
                        "acml_tr_pbmn": "700000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="market_cap")

        assert result["ranking_type"] == "market_cap"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"

    async def test_kr_foreigners_ranking_fallback_to_mksc_shrn_iscd(self, monkeypatch):
        """foreigners 랭킹에서 mksc_shrn_iscd fallback 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "20000000",
                        "frgn_ntby_tr_pbmn": "700000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"
        assert result["source"] == "kis"

    async def test_kr_gainers_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                if direction == "up":
                    return [
                        {
                            "stck_shrn_iscd": "005930",
                            "hts_kor_isnm": "삼성전자",
                            "stck_prpr": "80000",
                            "prdy_ctrt": "5.0",
                            "acml_vol": "10000000",
                            "hts_avls": "100000000000000",
                            "acml_tr_pbmn": "800000000000000",
                        }
                    ]
                return []

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="gainers")

        assert result["ranking_type"] == "gainers"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["change_rate"] == pytest.approx(5.0)

    async def test_kr_gainers_missing_ranking_fields_are_honest_null(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                if direction == "up":
                    return [
                        {
                            "stck_shrn_iscd": "005930",
                            "hts_kor_isnm": "삼성전자",
                            "stck_prpr": "80000",
                            "prdy_ctrt": "5.0",
                        }
                    ]
                return []

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="gainers")
        row = result["rankings"][0]

        assert row["price"] == pytest.approx(80000.0)
        assert row["volume"] is None
        assert row["market_cap"] is None
        assert row["trade_amount"] is None

    async def test_kr_losers_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                if direction == "down":
                    return [
                        {
                            "stck_shrn_iscd": "035420",
                            "hts_kor_isnm": "삼성SDS",
                            "stck_prpr": "70000",
                            "prdy_ctrt": "-3.0",
                            "acml_vol": "5000000",
                            "hts_avls": "50000000000000",
                            "acml_tr_pbmn": "350000000000000",
                        }
                    ]
                return []

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="losers")

        assert result["ranking_type"] == "losers"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["change_rate"] == pytest.approx(-3.0)

    async def test_kr_foreigners_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "10000000",
                        "frgn_ntby_tr_pbmn": "800000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 1

    async def test_kr_market_cap_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def market_cap_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="market_cap")

        assert result["ranking_type"] == "market_cap"
        assert len(result["rankings"]) == 1

    async def test_kr_market_cap_uses_stck_avls_fallback(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def market_cap_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "stck_avls": "470000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="market_cap")

        assert result["rankings"][0]["market_cap"] == pytest.approx(470000000000000.0)

    async def test_unsupported_market_ranking_combination(self):
        tools = build_tools()

        result = await tools["get_top_stocks"](market="kr", ranking_type="invalid_type")

        assert "error" in result
        assert result["source"] == "validation"

    async def test_limit_clamping(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [{"stck_shrn_iscd": "005930"}] * 100

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="volume", limit=10
        )

        assert result["total_count"] == 10
        assert len(result["rankings"]) == 10

    async def test_limit_min_clamp(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return []

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="volume", limit=0
        )

        assert result["total_count"] == 0
        assert len(result["rankings"]) == 0

    async def test_schema_smoke(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "2.5",
                        "acml_vol": "10000000",
                        "hts_avls": "100000000000000",
                        "acml_tr_pbmn": "800000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="volume", limit=1
        )

        assert "rankings" in result
        assert "total_count" in result
        assert "market" in result
        assert "ranking_type" in result
        assert "timestamp" in result
        assert "source" in result

        ranking = result["rankings"][0]
        assert "rank" in ranking
        assert "symbol" in ranking
        assert "name" in ranking
        assert "price" in ranking
        assert "change_rate" in ranking
        assert "volume" in ranking
        assert "market_cap" in ranking
        assert "trade_amount" in ranking

        assert result["total_count"] == 1
        assert result["total_count"] == len(result["rankings"])
        assert result["rankings"][0]["rank"] == 1

    async def test_us_rankings_volume(self, monkeypatch):
        tools = build_tools()

        import pandas as pd

        mock_df = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT", "GOOGL"],
                "longName": ["Apple Inc.", "Microsoft Corp.", "Alphabet Inc."],
                "regularMarketPrice": [150.0, 250.0, 130.0],
                "previousClose": [148.0, 245.0, 128.0],
                "regularMarketVolume": [50000000, 40000000, 30000000],
                "marketCap": [2000000000000, 1500000000000, 1000000000000],
            }
        )

        def mock_screen(*args, **kwargs):
            assert kwargs.get("session") is not None
            return mock_df

        monkeypatch.setattr(yf, "screen", mock_screen)

        result = await tools["get_top_stocks"](
            market="us", ranking_type="volume", limit=2
        )

        assert result["market"] == "us"
        assert result["ranking_type"] == "volume"
        assert result["total_count"] == 2
        assert result["rankings"][0]["symbol"] == "AAPL"

    async def test_us_rankings_market_cap(self, monkeypatch):
        tools = build_tools()

        import pandas as pd

        mock_df = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "longName": ["Apple Inc.", "Microsoft Corp."],
                "regularMarketPrice": [150.0, 250.0],
                "regularMarketVolume": [50000000, 40000000],
                "marketCap": [2000000000000, 1500000000000],
            }
        )

        mock_query = MagicMock()
        monkeypatch.setattr(yf, "EquityQuery", lambda *args, **kw: mock_query)

        def mock_screen(*args, **kwargs):
            assert kwargs.get("session") is not None
            return mock_df

        monkeypatch.setattr(yf, "screen", mock_screen)

        result = await tools["get_top_stocks"](
            market="us", ranking_type="market_cap", limit=2
        )

        assert result["ranking_type"] == "market_cap"
        assert result["total_count"] == 2
        assert len(result["rankings"]) == 2
        assert result["source"] == "yfinance"

    async def test_us_rankings_market_cap_exception_source(self, monkeypatch):
        tools = build_tools()

        def mock_screen_raises(*args, **kw):
            assert kw.get("session") is not None
            raise RuntimeError("yfinance API error")

        monkeypatch.setattr(yf, "screen", mock_screen_raises)

        result = await tools["get_top_stocks"](
            market="us", ranking_type="market_cap", limit=2
        )

        assert "error" in result
        assert result["source"] == "yfinance"
        assert "yfinance API error" in result["error"]

    async def test_us_market_cap_yf_screen_call_params(self, monkeypatch):
        """US market_cap 시 yf.screen이 올바른 인자로 호출되는지 검증"""
        tools = build_tools()

        import pandas as pd

        mock_df = pd.DataFrame(
            {
                "symbol": ["AAPL"],
                "longName": ["Apple Inc."],
                "regularMarketPrice": [150.0],
                "regularMarketVolume": [50000000],
                "marketCap": [2000000000000],
            }
        )

        screen_call_params = []

        def mock_screen(*args, **kwargs):
            screen_call_params.append({"args": args, "kwargs": kwargs})
            return mock_df

        mock_query = MagicMock()
        monkeypatch.setattr(yf, "EquityQuery", lambda *args, **kw: mock_query)
        monkeypatch.setattr(yf, "screen", mock_screen)

        await tools["get_top_stocks"](market="us", ranking_type="market_cap", limit=10)

        assert len(screen_call_params) == 1
        call_kwargs = screen_call_params[0]["kwargs"]
        assert call_kwargs["session"] is not None
        assert call_kwargs["size"] == 10
        assert call_kwargs["sortField"] == "intradaymarketcap"
        assert call_kwargs["sortAsc"] is False

    async def test_crypto_rankings_volume(self, monkeypatch):
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "0.025",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000000",
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": "4000000",
                    "signed_change_rate": "0.03",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "320000000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="volume", limit=2
        )

        assert result["market"] == "crypto"
        assert result["ranking_type"] == "volume"
        assert result["total_count"] == 2
        assert result["rankings"][0]["symbol"] == "KRW-BTC"

    async def test_crypto_rankings_fills_market_cap_from_coingecko(self, monkeypatch):
        """ROB-369 B5 — get_top_stocks(crypto) left market_cap=null for every
        row; it now enriches from the same CoinGecko cache screen_stocks uses."""
        from app.mcp_server.tooling.screening import crypto as screening_crypto

        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "0.025",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000000",
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": "4000000",
                    "signed_change_rate": "0.03",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "320000000000",
                },
            ]

        async def mock_coingecko_fetch():
            return {
                "data": {
                    "BTC": {"market_cap": 3_000_000_000_000_000, "market_cap_rank": 1},
                    "ETH": {"market_cap": 500_000_000_000_000, "market_cap_rank": 2},
                },
                "cached": True,
                "age_seconds": 1.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            screening_crypto, "_run_crypto_coingecko_fetch", mock_coingecko_fetch
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="volume", limit=2
        )
        rankings = result["rankings"]
        btc = next(r for r in rankings if r["symbol"] == "KRW-BTC")
        eth = next(r for r in rankings if r["symbol"] == "KRW-ETH")
        assert btc["market_cap"] == 3_000_000_000_000_000
        assert eth["market_cap"] == 500_000_000_000_000
        # trade_amount stays populated (was never the bug).
        assert btc["trade_amount"] == pytest.approx(8_000_000_000_000.0)

    async def test_crypto_rankings_gainers_sort(self, monkeypatch):
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-ETH",
                    "trade_price": "4000000",
                    "signed_change_rate": "0.05",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "320000000000",
                },
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "0.025",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="gainers", limit=2
        )

        assert result["ranking_type"] == "gainers"
        assert len(result["rankings"]) == 2
        assert result["rankings"][0]["symbol"] == "KRW-ETH"
        assert result["rankings"][0]["change_rate"] == pytest.approx(5.0)

    async def test_crypto_rankings_losers_sort(self, monkeypatch):
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "-0.01",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000000",
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": "4000000",
                    "signed_change_rate": "-0.005",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "320000000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="losers", limit=2
        )

        assert result["ranking_type"] == "losers"
        assert len(result["rankings"]) == 2
        assert result["rankings"][0]["symbol"] == "KRW-BTC"
        assert result["rankings"][0]["change_rate"] == pytest.approx(-1.0)

    async def test_crypto_rankings_relative_strength_sort_excludes_btc(
        self, monkeypatch
    ):
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "100000000",
                    "signed_change_rate": "0.03",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "10000000000",
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": "5000000",
                    "signed_change_rate": "0.05",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "20000000000",
                },
                {
                    "market": "KRW-XRP",
                    "trade_price": "900",
                    "signed_change_rate": "0.04",
                    "acc_trade_volume_24h": "200",
                    "acc_trade_price_24h": "30000000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto",
            ranking_type="relative_strength",
            limit=5,
        )

        assert result["ranking_type"] == "relative_strength"
        assert [row["symbol"] for row in result["rankings"]] == ["KRW-ETH", "KRW-XRP"]
        assert result["rankings"][0]["relative_strength_vs_btc_24h"] == pytest.approx(
            0.02
        )
        assert result["rankings"][0][
            "relative_strength_pct_vs_btc_24h"
        ] == pytest.approx(2.0)

    async def test_get_crypto_top_movers_defaults_to_relative_strength(
        self, monkeypatch
    ):
        tools = build_tools()
        assert "get_crypto_top_movers" in tools

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "100000000",
                    "signed_change_rate": "0.01",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "10000000000",
                },
                {
                    "market": "KRW-SOL",
                    "trade_price": "220000",
                    "signed_change_rate": "0.04",
                    "acc_trade_volume_24h": "90",
                    "acc_trade_price_24h": "9000000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_crypto_top_movers"](limit=10)

        assert result["market"] == "crypto"
        assert result["ranking_type"] == "relative_strength"
        assert result["rankings"][0]["symbol"] == "KRW-SOL"

    async def test_crypto_ratio_to_percent_conversion(self, monkeypatch):
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "0.025",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000000",
                }
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="volume", limit=1
        )

        assert result["rankings"][0]["change_rate"] == pytest.approx(2.5)

    async def test_upstream_exception_returns_error_payload(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                raise RuntimeError("KIS API error")

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="volume")

        assert "error" in result
        assert "source" in result
        assert "KIS API error" in result["error"]

    async def test_upbit_exception_returns_error_payload(self, monkeypatch):
        tools = build_tools()

        class MockUpbitService:
            async def fetch_top_traded_coins(self):
                raise RuntimeError("Upbit API error")

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            MockUpbitService().fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](market="crypto", ranking_type="volume")

        assert "error" in result
        assert "source" in result
        assert result["source"] == "upbit"
        assert "Upbit API error" in result["error"]

    async def test_kr_foreigners_ranking_foreign_specific_fields(self, monkeypatch):
        """ROB-629: foreigners ranking surfaces foreign net flow as NAMED fields
        (foreign_net_qty / foreign_net_amount) and no longer stuffs them into the
        generic volume / trade_amount slots. hts_avls is NOT fabricated — the real
        KIS foreign ranking does not return it, so market_cap is honestly null."""
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "1.5",
                        "frgn_ntby_qty": "3000000",
                        "frgn_ntby_tr_pbmn": "360000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 2

        first = result["rankings"][0]
        assert first["symbol"] == "005930"
        assert first["name"] == "삼성전자"
        # Named foreign fields — the whole point of ROB-629.
        assert first["foreign_net_qty"] == 5000000
        assert first["foreign_net_amount"] == pytest.approx(400000000000.0)
        # Generic slots are NO LONGER stuffed with the foreign values.
        assert first["volume"] is None
        assert first["trade_amount"] is None
        # market_cap honestly null (hts_avls not returned by the foreign ranking).
        assert first["market_cap"] is None

        second = result["rankings"][1]
        assert second["symbol"] == "005380"
        assert second["name"] == "LG전자"
        assert second["foreign_net_qty"] == 3000000
        assert second["foreign_net_amount"] == pytest.approx(360000000000.0)
        assert second["volume"] is None
        assert second["trade_amount"] is None

    async def test_kr_foreign_net_buy_and_sell_split_dispatch(self, monkeypatch):
        """ROB-629: foreign_net_buy passes FID rank_sort '0' (net buy),
        foreign_net_sell passes '1' (net sell); 'foreigners' aliases
        foreign_net_buy. Response echoes the caller's original ranking_type."""
        tools = build_tools()

        captured: list[str] = []

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                captured.append(rank_sort)
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        buy = await tools["get_top_stocks"](market="kr", ranking_type="foreign_net_buy")
        assert buy["ranking_type"] == "foreign_net_buy"
        assert len(buy["rankings"]) == 1

        sell = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_sell"
        )
        assert sell["ranking_type"] == "foreign_net_sell"
        assert len(sell["rankings"]) == 1

        alias = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert alias["ranking_type"] == "foreigners"
        assert len(alias["rankings"]) == 1

        # net buy -> "0", net sell -> "1", foreigners alias -> "0".
        assert captured == ["0", "1", "0"]


@pytest.mark.asyncio
class TestMCPLosers:
    async def test_get_top_stocks_kr_losers_returns_only_negatives(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "stck_prpr": "70000",
                        "prdy_ctrt": "-3.0",
                        "acml_vol": "5000000",
                        "hts_avls": "50000000000000",
                        "acml_tr_pbmn": "350000000000000",
                    },
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "-2.0",
                        "acml_vol": "2000000",
                        "hts_avls": "200000000000000",
                        "acml_tr_pbmn": "160000000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="losers", limit=5
        )

        assert result["market"] == "kr"
        assert result["ranking_type"] == "losers"
        assert len(result["rankings"]) == 2
        assert all(float(r["change_rate"]) < 0 for r in result["rankings"])
        assert float(result["rankings"][0]["change_rate"]) == pytest.approx(-3.0)
        assert float(result["rankings"][1]["change_rate"]) == pytest.approx(-2.0)

    async def test_min_market_cap_drops_only_known_junk_cap_rows(self, monkeypatch):
        """ROB-976: min_market_cap cuts rows with a KNOWN market_cap below the
        floor, but never drops a row whose market_cap KIS simply omitted
        (honest, never fabricated exclusion)."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {  # big-cap loser — kept
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "-2.0",
                        "acml_vol": "2000000",
                        "hts_avls": "200000000000000",
                    },
                    {  # junk-cap loser — excluded
                        "stck_shrn_iscd": "900001",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "500",
                        "prdy_ctrt": "-9.0",
                        "acml_vol": "1000000",
                        "hts_avls": "5000000000",
                    },
                    {  # market_cap omitted by KIS — kept (unknown, not fabricated-excluded)
                        "stck_shrn_iscd": "900002",
                        "hts_kor_isnm": "미확인",
                        "stck_prpr": "1000",
                        "prdy_ctrt": "-1.0",
                        "acml_vol": "500000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr",
            ranking_type="losers",
            limit=5,
            min_market_cap=30_000_000_000.0,
        )

        symbols = [r["symbol"] for r in result["rankings"]]
        assert symbols == ["005930", "900002"]
        assert result["market_cap_filter"] == {
            "min_market_cap": 30_000_000_000.0,
            "excluded_count": 1,
        }

    async def test_min_market_cap_omitted_keeps_prior_behavior(self, monkeypatch):
        """No min_market_cap -> no filter key in the response, behavior unchanged."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "-2.0",
                        "acml_vol": "2000000",
                        "hts_avls": "1000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="losers", limit=5
        )

        assert "market_cap_filter" not in result
        assert len(result["rankings"]) == 1

    async def test_min_market_cap_backfills_when_kis_omits_hts_avls(self, monkeypatch):
        """ROB-976 verify R1 [BLOCKER]: real KIS losers responses reproduced in
        the 07-20 verify report omit hts_avls entirely, making the bare filter
        a no-op. market_cap must be backfilled (same pipeline as the foreign
        rankings) before the floor is applied."""
        from decimal import Decimal as _D

        from app.mcp_server.tooling import foreigners_liquidity

        async def fake_fetch(symbols, *, session_factory=None):
            return (
                {"900001": _D("5000000000"), "900002": _D("400000000000")},
                {},
            )

        monkeypatch.setattr(foreigners_liquidity, "_fetch_market_cap_maps", fake_fetch)

        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {  # junk cap once backfilled (50억) — excluded
                        "stck_shrn_iscd": "900001",
                        "hts_kor_isnm": "좋은사람들",
                        "stck_prpr": "500",
                        "prdy_ctrt": "-9.0",
                        "acml_vol": "1000000",
                        # no hts_avls, matches the real KIS losers payload
                    },
                    {  # blue-chip cap once backfilled (4000억) — kept
                        "stck_shrn_iscd": "900002",
                        "hts_kor_isnm": "대형주",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "-2.0",
                        "acml_vol": "2000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr",
            ranking_type="losers",
            limit=5,
            min_market_cap=30_000_000_000.0,
        )

        symbols = [r["symbol"] for r in result["rankings"]]
        assert symbols == ["900002"]
        assert result["rankings"][0]["market_cap"] == pytest.approx(4e11)
        assert result["market_cap_filter"]["excluded_count"] == 1

    async def test_min_turnover_uses_trade_amount_then_price_times_volume(
        self, monkeypatch
    ):
        """ROB-976: min_turnover checks trade_amount (acml_tr_pbmn) first, and
        falls back to price*volume when KIS omits trade_amount — never drops a
        row with neither value available."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {  # trade_amount present, below the 10억 floor -> excluded
                        "stck_shrn_iscd": "900001",
                        "hts_kor_isnm": "저유동성",
                        "stck_prpr": "1000",
                        "prdy_ctrt": "-3.0",
                        "acml_vol": "100000",
                        "acml_tr_pbmn": "100000000",  # 1억
                    },
                    {  # no trade_amount; price*volume = 80000*2000000 = 1600억 -> kept
                        "stck_shrn_iscd": "900002",
                        "hts_kor_isnm": "대형주",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "-1.0",
                        "acml_vol": "2000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr",
            ranking_type="losers",
            limit=5,
            min_turnover=1_000_000_000.0,
        )

        symbols = [r["symbol"] for r in result["rankings"]]
        assert symbols == ["900002"]
        assert result["turnover_filter"] == {
            "min_turnover": 1_000_000_000.0,
            "excluded_count": 1,
        }

    async def test_quality_filter_emptying_losers_is_degraded_not_bullish_message(
        self, monkeypatch
    ):
        """ROB-976 verify R1 [BLOCKER]: when the quality floor removes every
        real loser, the response must say so (status=degraded) — not the
        generic 'market may be entirely bullish' message, which would hide
        that a filter (not market conditions) produced the empty page."""
        from decimal import Decimal as _D

        from app.mcp_server.tooling import foreigners_liquidity

        async def fake_fetch(symbols, *, session_factory=None):
            return ({s: _D("5000000000") for s in symbols}, {})

        monkeypatch.setattr(foreigners_liquidity, "_fetch_market_cap_maps", fake_fetch)

        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "900001",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "500",
                        "prdy_ctrt": "-9.0",
                        "acml_vol": "1000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr",
            ranking_type="losers",
            limit=5,
            min_market_cap=30_000_000_000.0,
        )

        assert result["rankings"] == []
        assert result["status"] == "degraded"
        assert "degraded_reason" in result
        assert "error" not in result

    async def test_get_top_stocks_kr_gainers_returns_positives(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "5.0",
                        "acml_vol": "10000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="gainers", limit=5
        )

        assert result["market"] == "kr"
        assert result["ranking_type"] == "gainers"
        assert len(result["rankings"]) == 1
        assert float(result["rankings"][0]["change_rate"]) > 0

    async def test_kr_gainers_premarket_suppresses_zero_garbage(self, monkeypatch):
        """ROB-464: pre-market KRX gainers come back all-zero/alphabetical garbage.
        Suppress the fake-0 rows and tag data_state instead of presenting them."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "000020",
                        "hts_kor_isnm": "동화약품",
                        "stck_prpr": "10000",
                        "prdy_ctrt": "0.00",
                        "acml_vol": "0",
                    },
                    {
                        "stck_shrn_iscd": "000040",
                        "hts_kor_isnm": "KR모터스",
                        "stck_prpr": "2000",
                        "prdy_ctrt": "0.00",
                        "acml_vol": "0",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "premarket_unavailable",
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="gainers")

        assert result["data_state"] == "premarket_unavailable"
        assert result["rankings"] == []
        assert result["total_count"] == 0
        assert result.get("note")

    async def test_kr_losers_premarket_empty_suppressed_with_data_state(
        self, monkeypatch
    ):
        """ROB-464: pre-market losers filter to empty; return a premarket data_state
        payload, not the legacy 'No losing stocks found' bullish-market error."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "000020",
                        "hts_kor_isnm": "동화약품",
                        "prdy_ctrt": "0.00",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "premarket_unavailable",
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="losers")

        assert result["data_state"] == "premarket_unavailable"
        assert result["rankings"] == []
        assert "error" not in result

    async def test_kr_gainers_fresh_keeps_rankings_and_tags_fresh(self, monkeypatch):
        """ROB-464: during the regular session, real movers are kept and tagged fresh."""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "5.0",
                        "acml_vol": "10000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="gainers")

        assert result["data_state"] == "fresh"
        assert len(result["rankings"]) == 1


@pytest.mark.asyncio
class TestMCPEmptyLosersErrors:
    """Tests for empty losers error payloads"""

    async def test_get_top_stocks_kr_losers_empty_returns_error_payload(
        self, monkeypatch
    ):
        """Empty losers results should return explicit error payload"""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                # Return only positives (no losers)
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "prdy_ctrt": "1.0",
                    },
                    {
                        "stck_shrn_iscd": "000660",
                        "hts_kor_isnm": "SK하이닉스",
                        "prdy_ctrt": "2.0",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        # Premise: a fresh, bullish trading session (no losers), not pre-market.
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="losers", limit=5
        )

        assert "error" in result
        assert result["source"] == "kis"
        assert "market=kr, ranking_type=losers" in result["query"]
        assert "No losing stocks found" in result["error"]
        assert "KIS API limitation" in result["error"]

    async def test_get_top_stocks_kr_losers_non_empty_returns_rankings(
        self, monkeypatch
    ):
        """Losers with actual negatives should return rankings, not error"""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                # Return actual negatives
                return [
                    {
                        "stck_shrn_iscd": "035420",
                        "hts_kor_isnm": "삼성SDS",
                        "prdy_ctrt": "-3.0",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "prdy_ctrt": "-1.5",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="losers", limit=5
        )

        assert "error" not in result
        assert len(result["rankings"]) == 2
        assert all(float(r["change_rate"]) < 0 for r in result["rankings"])


@pytest.mark.asyncio
class TestMCPRegressionTests:
    """Regression tests to ensure existing functionality is not broken"""

    async def test_kr_gainers_unchanged(self, monkeypatch):
        """KR gainers should return only positives, sorted by change_rate descending"""
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "prdy_ctrt": "5.0",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "prdy_ctrt": "3.0",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="gainers", limit=5
        )

        assert result["market"] == "kr"
        assert result["ranking_type"] == "gainers"
        assert len(result["rankings"]) == 2
        assert result["rankings"][0]["symbol"] == "005930"
        assert float(result["rankings"][0]["change_rate"]) == pytest.approx(5.0)

    async def test_us_losers_unchanged(self, monkeypatch):
        """US losers should return only negatives, sorted by change_rate ascending"""
        tools = build_tools()

        import pandas as pd

        mock_df = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT", "GOOGL"],
                "longName": ["Apple Inc.", "Microsoft Corp.", "Alphabet Inc."],
                "regularMarketPrice": [150.0, 250.0, 130.0],
                "previousClose": [
                    152.0,
                    245.0,
                    135.0,
                ],  # Add previousClose for change_rate calc
                "regularMarketVolume": [50000000, 40000000, 30000000],
            }
        )

        def mock_screen(*args, **kwargs):
            assert kwargs.get("session") is not None
            return mock_df

        monkeypatch.setattr(yf, "screen", mock_screen)

        result = await tools["get_top_stocks"](
            market="us", ranking_type="losers", limit=5
        )

        assert result["market"] == "us"
        assert result["ranking_type"] == "losers"
        # MSFT is positive (+2.0%) so filtered out: only GOOGL and AAPL returned
        assert len(result["rankings"]) == 2
        # Sorted by change_rate ascending: GOOGL (-3.7%) before AAPL (-1.3%)
        assert result["rankings"][0]["symbol"] == "GOOGL"  # -3.7%
        assert result["rankings"][1]["symbol"] == "AAPL"  # -1.3%

    async def test_crypto_losers_unchanged(self, monkeypatch):
        """Crypto losers should return only negatives, sorted by change_rate ascending"""
        tools = build_tools()

        async def mock_fetch_top_traded_coins():
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": "80000000",
                    "signed_change_rate": "-0.01",
                    "acc_trade_volume_24h": "100",
                    "acc_trade_price_24h": "8000000000",
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": "4000000",
                    "signed_change_rate": "-0.02",
                    "acc_trade_volume_24h": "80",
                    "acc_trade_price_24h": "32000000",
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await tools["get_top_stocks"](
            market="crypto", ranking_type="losers", limit=5
        )

        assert result["market"] == "crypto"
        assert result["ranking_type"] == "losers"
        assert len(result["rankings"]) == 2
        # Sorted by change_rate ascending: -0.02 before -0.01
        assert result["rankings"][0]["symbol"] == "KRW-ETH"
        assert result["rankings"][0]["change_rate"] == pytest.approx(
            -2.0
        )  # -0.02 * 100
        assert result["rankings"][1]["symbol"] == "KRW-BTC"
        assert result["rankings"][1]["change_rate"] == pytest.approx(
            -1.0
        )  # -0.01 * 100


@pytest.mark.asyncio
class TestForeignersLiquidity:
    async def _patch_fetch(self, monkeypatch, snapshot_caps=None, shares=None):
        from decimal import Decimal as _D

        from app.mcp_server.tooling import foreigners_liquidity

        async def fake_fetch(symbols, *, session_factory=None):
            return (
                {k: _D(str(v)) for k, v in (snapshot_caps or {}).items()},
                {k: _D(str(v)) for k, v in (shares or {}).items()},
            )

        monkeypatch.setattr(foreigners_liquidity, "_fetch_market_cap_maps", fake_fetch)

    async def test_backfill_wired_from_snapshot(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch, snapshot_caps={"005930": 4e14})

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        row = result["rankings"][0]
        assert row["market_cap"] == 4e14
        assert row["market_cap_source"] == "fundamentals_snapshot"
        assert result["liquidity_filter"]["include_illiquid"] is False
        assert result["liquidity_filter"]["excluded_count"] == 0

    async def test_filter_excludes_junk_default_on(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)  # no caps -> null

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    },
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_qty": "1000",
                        "frgn_ntby_tr_pbmn": "300000",  # 30만 KRW — junk
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert [r["symbol"] for r in result["rankings"]] == ["005930"]
        assert result["rankings"][0]["rank"] == 1
        assert result["liquidity_filter"]["excluded_count"] == 1

    async def test_include_illiquid_keeps_all(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_tr_pbmn": "300000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](
            market="kr", ranking_type="foreigners", include_illiquid=True
        )
        assert len(result["rankings"]) == 1
        assert result["liquidity_filter"]["include_illiquid"] is True
        assert result["liquidity_filter"]["excluded_count"] == 0

    async def test_filter_empties_sets_degraded(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_tr_pbmn": "300000",  # below threshold
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert result["rankings"] == []
        assert result["total_count"] == 0
        assert result["status"] == "degraded"
        assert "liquidity threshold" in result["degraded_reason"]
        assert result["liquidity_filter"]["excluded_count"] == 1

    async def test_foreigners_offsession_fake_zero_flow_suppressed(self, monkeypatch):
        """T1: off-session the KIS foreign-buying-rank returns fake-0 가집계 rows
        (no real net flow). When data_state is NON-fresh and no row carries real
        foreign flow, the guard suppresses the fake-0 rows and tags data_state —
        never presenting 가집계 zeros as live foreign flow."""
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "0.00",
                        "frgn_ntby_qty": "0",
                        "frgn_ntby_tr_pbmn": "0",
                    },
                    {
                        "stck_shrn_iscd": "000660",
                        "hts_kor_isnm": "SK하이닉스",
                        "stck_prpr": "180000",
                        "prdy_ctrt": "0.00",
                        "frgn_ntby_qty": "0",
                        "frgn_ntby_tr_pbmn": "0",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "premarket_unavailable",
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["data_state"] == "premarket_unavailable"
        assert result["rankings"] == []
        assert result["total_count"] == 0
        assert result.get("note")
        # Suppressed BEFORE the liquidity filter ran — no liquidity meta attached.
        assert "liquidity_filter" not in result

    async def test_foreigners_offsession_real_flow_not_suppressed(self, monkeypatch):
        """T1 positive counterpart: NON-fresh data_state but a row carries real
        foreign net flow (has_real_flow=True) must NOT be suppressed — the guard
        only drops the all-fake-0 case."""
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "premarket_unavailable",
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        # Real flow survives; data_state is still tagged honestly as non-fresh.
        assert result["data_state"] == "premarket_unavailable"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "005930"
        assert result["rankings"][0]["foreign_net_amount"] == pytest.approx(4e11)
        assert "note" not in result
