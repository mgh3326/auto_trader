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

    def tool(self, name: str, description: str, **_options):
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

        async def _no_normalized_caps(*args, **kwargs):
            return {}

        monkeypatch.setattr(
            analysis_tool_handlers,
            "fetch_normalized_kr_market_caps",
            _no_normalized_caps,
        )

    async def test_get_top_stocks_us_uses_analysis_screening_rankings_alias(
        self, monkeypatch
    ):
        tools = build_tools()

        async def fake_get_us_rankings(ranking_type: str, limit: int):
            assert ranking_type == "volume"
            # The default-ON US quality bar over-fetches (limit * 4, cap 100).
            assert limit == 12
            return (
                [
                    {
                        "rank": 1,
                        "symbol": "AAPL",
                        "name": "Apple",
                        "market_cap": 3_000_000_000_000,
                        "trade_amount": 5_000_000_000,
                    }
                ],
                "shim-us",
            )

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
                        "frgn_ntby_tr_pbmn": "700000",  # 백만원
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
                        "frgn_ntby_tr_pbmn": "800000",  # 백만원
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
        # The default-ON US quality bar over-fetches: limit 10 -> fetch 40.
        assert call_kwargs["size"] == 40
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
                        "frgn_ntby_tr_pbmn": "400000",  # 백만원
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "1.5",
                        "frgn_ntby_qty": "3000000",
                        "frgn_ntby_tr_pbmn": "360000",  # 백만원
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
                        "frgn_ntby_tr_pbmn": "400000",  # 백만원
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

    async def test_min_market_cap_is_fail_closed_on_normalized_snapshot(
        self, monkeypatch
    ):
        """A requested quality floor accepts only normalized snapshot coverage."""
        import datetime as _dt
        from decimal import Decimal as _D

        from app.services.market_valuation_snapshots.normalized_market_cap import (
            NormalizedMarketCap,
        )

        async def fake_caps(symbols):
            return {
                "005930": NormalizedMarketCap(
                    _D("200000000000000"), _dt.date(2026, 7, 20), "naver_finance"
                ),
                "900001": NormalizedMarketCap(
                    _D("5000000000"), _dt.date(2026, 7, 20), "naver_finance"
                ),
            }

        monkeypatch.setattr(
            analysis_tool_handlers, "fetch_normalized_kr_market_caps", fake_caps
        )
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
                    {  # normalized snapshot omitted — fail-closed
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
        assert symbols == ["005930"]
        assert result["market_cap_filter"] == {
            "min_market_cap": 30_000_000_000.0,
            "excluded_count": 2,
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
        a no-op. market_cap must come from the normalized Naver valuation
        snapshot before the floor is applied."""
        import datetime as _dt
        from decimal import Decimal as _D

        from app.services.market_valuation_snapshots.normalized_market_cap import (
            NormalizedMarketCap,
        )

        async def fake_fetch(symbols):
            return {
                "900001": NormalizedMarketCap(
                    _D("5000000000"), _dt.date(2026, 7, 20), "naver_finance"
                ),
                "900002": NormalizedMarketCap(
                    _D("400000000000"), _dt.date(2026, 7, 20), "naver_finance"
                ),
            }

        monkeypatch.setattr(
            analysis_tool_handlers, "fetch_normalized_kr_market_caps", fake_fetch
        )

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

    async def test_min_market_cap_keeps_real_large_cap_with_r2_payload_shape(
        self, monkeypatch
    ):
        """R2 regression: 삼성생명 must use 60.3조 KRW, not the ~399억
        provider-unit value that previously caused a false exclusion."""
        import datetime as _dt
        from decimal import Decimal as _D

        from app.services.market_valuation_snapshots.normalized_market_cap import (
            NormalizedMarketCap,
        )

        async def fake_caps(symbols):
            return {
                "032830": NormalizedMarketCap(
                    _D("60300000000000"), _dt.date(2026, 7, 20), "naver_finance"
                )
            }

        monkeypatch.setattr(
            analysis_tool_handlers, "fetch_normalized_kr_market_caps", fake_caps
        )
        tools = build_tools()

        class MockKISClient:
            async def fluctuation_rank(self, market, direction, limit):
                return [
                    {
                        "stck_shrn_iscd": "032830",
                        "hts_kor_isnm": "삼성생명",
                        "stck_prpr": "301500",
                        "prdy_ctrt": "-1.2",
                        "acml_vol": "1000000",
                        # Reproduce the unnormalized R2 value; it must be ignored.
                        "hts_avls": "39943178204",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr",
            ranking_type="losers",
            min_market_cap=300_000_000_000.0,
        )

        assert [row["symbol"] for row in result["rankings"]] == ["032830"]
        assert result["rankings"][0]["market_cap"] == pytest.approx(60.3e12)
        assert result["rankings"][0]["market_cap_source"].endswith("naver_finance")

    async def test_min_turnover_uses_trade_amount_then_price_times_volume(
        self, monkeypatch
    ):
        """ROB-976: min_turnover checks trade_amount (acml_tr_pbmn) first, and
        falls back to price*volume when KIS omits trade_amount; rows with neither
        value fail the requested quality gate."""
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
        import datetime as _dt
        from decimal import Decimal as _D

        from app.services.market_valuation_snapshots.normalized_market_cap import (
            NormalizedMarketCap,
        )

        async def fake_fetch(symbols):
            return {
                symbol: NormalizedMarketCap(
                    _D("5000000000"), _dt.date(2026, 7, 20), "naver_finance"
                )
                for symbol in symbols
            }

        monkeypatch.setattr(
            analysis_tool_handlers, "fetch_normalized_kr_market_caps", fake_fetch
        )

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
                # Required by the default-ON US quality bar (missing cap would
                # fail closed and drop every row).
                "marketCap": [3_000_000_000_000, 2_500_000_000_000, 2_000_000_000_000],
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
                        "frgn_ntby_tr_pbmn": "400000",  # 백만원
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
                        "frgn_ntby_tr_pbmn": "400000",  # 백만원
                    },
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_qty": "1000",
                        "frgn_ntby_tr_pbmn": "30",  # 30 백만원 = 3천만 KRW, junk
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
                        "frgn_ntby_tr_pbmn": "30",
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
                        "frgn_ntby_tr_pbmn": "30",  # 3천만 KRW, below threshold
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
                        "frgn_ntby_tr_pbmn": "400000",  # 백만원
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

    # #1029: 2026-09-29 20:56 KST post-close observation. Toss Discover showed
    # these foreign net buys; KIS FHPTJ04400000 reports frgn_ntby_tr_pbmn in
    # 백만원, so 925억 arrives as "92500". Reading it as raw KRW dropped every
    # row below the 1억 floor and returned status=degraded.
    _POST_CLOSE_ROWS_20260929 = (
        # (code, name, price, amount in 백만원)
        ("042700", "한미반도체", "125000", "92500"),
        ("001820", "삼화콘덴서", "52000", "43000"),
        ("036930", "주성엔지니어링", "41000", "36200"),
        ("403870", "HPSP", "38500", "30100"),
        ("222800", "심텍", "33000", "25400"),
    )

    def _kis_rows_20260929(self, *, sign: str = "") -> list[dict[str, str]]:
        return [
            {
                "mksc_shrn_iscd": code,
                "hts_kor_isnm": name,
                "stck_prpr": price,
                "prdy_ctrt": "2.10",
                # qty x current price == amount (the documented derivation).
                "frgn_ntby_qty": f"{sign}{int(amount) * 1_000_000 // int(price)}",
                "frgn_ntby_tr_pbmn": f"{sign}{amount}",
            }
            for code, name, price, amount in self._POST_CLOSE_ROWS_20260929
        ]

    async def test_1029_post_close_documented_unit_rows_survive(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)
        rows = self._kis_rows_20260929()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return rows

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "market_closed",
        )

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_buy", limit=8
        )

        assert "status" not in result
        assert "degraded_reason" not in result
        assert result["data_state"] == "market_closed"
        assert result["liquidity_filter"]["excluded_count"] == 0
        assert [r["symbol"] for r in result["rankings"]] == [
            "042700",
            "001820",
            "036930",
            "403870",
            "222800",
        ]
        assert [r["foreign_net_amount"] for r in result["rankings"]] == [
            92_500_000_000.0,
            43_000_000_000.0,
            36_200_000_000.0,
            30_100_000_000.0,
            25_400_000_000.0,
        ]
        # qty x price reproduces the KRW amount — the unit is consistent.
        first = result["rankings"][0]
        assert first["foreign_net_qty"] * first["price"] == pytest.approx(
            first["foreign_net_amount"], rel=1e-5
        )
        # The source is a 가집계 tally: say so explicitly, not generic degraded.
        assert result["source_state"] == "provisional"
        assert (
            result["source_state_reason"]
            == "kis_foreign_institution_total_provisional_tally"
        )
        assert "14:30" in result["source_state_note"]
        assert result["foreign_net_amount_unit"] == "KRW"

    async def test_1029_net_sell_documented_unit_rows_survive(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)
        rows = self._kis_rows_20260929(sign="-")

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                assert rank_sort == "1"
                return rows

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_sell"
        )

        assert "status" not in result
        assert len(result["rankings"]) == 5
        assert result["rankings"][0]["foreign_net_amount"] == -92_500_000_000.0
        assert result["source_state"] == "provisional"

    async def test_1029_threshold_boundary_uses_documented_unit(self, monkeypatch):
        """1억 KRW == "100" 백만원: kept; "99" (9,900만 KRW): excluded."""
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "mksc_shrn_iscd": "000100",
                        "hts_kor_isnm": "경계통과",
                        "stck_prpr": "10000",
                        "frgn_ntby_qty": "10000",
                        "frgn_ntby_tr_pbmn": "100",
                    },
                    {
                        "mksc_shrn_iscd": "000099",
                        "hts_kor_isnm": "경계미달",
                        "stck_prpr": "10000",
                        "frgn_ntby_qty": "9900",
                        "frgn_ntby_tr_pbmn": "99",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert [r["symbol"] for r in result["rankings"]] == ["000100"]
        assert result["rankings"][0]["foreign_net_amount"] == 100_000_000.0
        assert result["liquidity_filter"]["excluded_count"] == 1
        assert result["liquidity_filter"]["min_foreign_net_amount_krw"] == 1e8

    async def test_1029_degraded_and_suppressed_shapes_carry_source_state(
        self, monkeypatch
    ):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)
        payload: list[dict[str, str]] = []

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return payload

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        # Liquidity-emptied (genuinely tiny flow) -> degraded, still provisional.
        payload[:] = [
            {
                "mksc_shrn_iscd": "900111",
                "hts_kor_isnm": "잡주",
                "stck_prpr": "300",
                "frgn_ntby_tr_pbmn": "30",
            }
        ]
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )
        degraded = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_buy"
        )
        assert degraded["status"] == "degraded"
        assert degraded["source_state"] == "provisional"
        assert degraded["foreign_net_amount_unit"] == "KRW"

        # Off-session fake-0 suppression, also provisional.
        payload[:] = [
            {
                "mksc_shrn_iscd": "005930",
                "hts_kor_isnm": "삼성전자",
                "stck_prpr": "80000",
                "frgn_ntby_qty": "0",
                "frgn_ntby_tr_pbmn": "0",
            }
        ]
        monkeypatch.setattr(
            analysis_tool_handlers,
            "kr_market_data_state",
            lambda *a, **k: "market_closed",
        )
        suppressed = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_buy"
        )
        assert suppressed["rankings"] == []
        assert suppressed["source_state"] == "provisional"

    async def test_1029_non_foreign_kr_ranking_has_no_source_state(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def volume_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "1000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="volume")
        assert len(result["rankings"]) == 1
        assert "source_state" not in result


# ---------------------------------------------------------------------------
# #922 / retro U-3 — the default-ON US quality bar for get_top_stocks.
# ---------------------------------------------------------------------------


def _us_mapped_row(
    symbol: str,
    name: str,
    *,
    market_cap: float | None = 3_000_000_000_000,
    price: float | None = 200.0,
    volume: int | None = 10_000_000,
    trade_amount: float | None = None,
    change_rate: float = -1.5,
    rank: int = 1,
) -> dict[str, Any]:
    """A mapped US ranking row as produced by analysis_screening._map_us_row."""
    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": change_rate,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


@pytest.mark.asyncio
class TestUSTopStocksQualityBar:
    """US rankings get the KR-mirrored fail-closed quality bar by default."""

    async def _run(
        self,
        monkeypatch,
        rows: list[dict[str, Any]],
        ranking_type: str = "losers",
        **kwargs: Any,
    ) -> dict[str, Any]:
        tools = build_tools()

        async def fake_get_us_rankings(rt: str, limit: int):
            assert rt == ranking_type
            return (list(rows), "yfinance-test")

        monkeypatch.setattr(
            analysis_screening, "_get_us_rankings", fake_get_us_rankings
        )
        return await tools["get_top_stocks"](
            market="us", ranking_type=ranking_type, **kwargs
        )

    async def test_us_default_floors_drop_sub_floor_cap_and_dead_turnover(
        self, monkeypatch
    ):
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("BIG", "Big Cap Co", market_cap=3e12),
                _us_mapped_row(
                    "SMALL", "Small Cap Co", market_cap=1_000_000_000
                ),  # below the 2e9 default
                _us_mapped_row(
                    "DEAD",
                    "Dead Turnover Co",
                    market_cap=5_000_000_000,
                    price=0.05,
                    volume=1_000,
                ),  # price*volume = 50 USD, below the 1e6 default
            ],
        )

        assert [row["symbol"] for row in result["rankings"]] == ["BIG"]
        assert result["market_cap_filter"] == {
            "min_market_cap": 2_000_000_000,
            "excluded_count": 1,
            "missing_market_cap_excluded_count": 0,
        }
        assert result["turnover_filter"] == {
            "min_turnover": 1_000_000,
            "excluded_count": 1,
        }
        assert result["instrument_filter"] == {
            "excluded_leveraged_inverse_etf_count": 0
        }

    async def test_us_market_cap_floor_boundary_is_inclusive(self, monkeypatch):
        """cap == floor keeps the row; floor - 1 drops it (fails on a > mutant)."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("EDGE", "Boundary Co", market_cap=2_000_000_000),
                _us_mapped_row("UNDER", "Under Co", market_cap=1_999_999_999),
            ],
            min_market_cap=2_000_000_000,
        )

        assert [row["symbol"] for row in result["rankings"]] == ["EDGE"]
        assert result["market_cap_filter"]["excluded_count"] == 1

    async def test_us_missing_market_cap_fails_closed_and_counted(self, monkeypatch):
        """Missing cap can never pass a floor — counted with its own reason."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("NOCAP", "No Cap Co", market_cap=None),
                _us_mapped_row("BIG", "Big Cap Co", market_cap=3e12),
            ],
        )

        assert [row["symbol"] for row in result["rankings"]] == ["BIG"]
        assert result["market_cap_filter"]["missing_market_cap_excluded_count"] == 1
        assert result["market_cap_filter"]["excluded_count"] == 1

    async def test_us_turnover_floor_boundary_and_fallback(self, monkeypatch):
        """trade_amount is authoritative; price*volume backfills it when absent."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("EXACT", "Exact Co", market_cap=5e9, trade_amount=100.0),
                _us_mapped_row("BELOW", "Below Co", market_cap=5e9, trade_amount=99.0),
                _us_mapped_row("CALC", "Calc Co", market_cap=5e9, price=2.0, volume=50),
                _us_mapped_row(
                    "CALCLO",
                    "Calc Low Co",
                    market_cap=5e9,
                    price=2.0,
                    volume=49,
                ),
                _us_mapped_row("NOVOL", "No Volume Co", market_cap=5e9, volume=None),
            ],
            min_turnover=100,
        )

        kept = {row["symbol"]: row for row in result["rankings"]}
        assert set(kept) == {"EXACT", "CALC"}
        # The price*volume fallback backfills the emitted row like KR does.
        assert kept["CALC"]["trade_amount"] == 100.0
        assert result["turnover_filter"]["excluded_count"] == 3

    async def test_us_leveraged_inverse_names_excluded(self, monkeypatch):
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("SOXL", "Direxion Daily Semiconductor Bull 3X Shares"),
                _us_mapped_row("SQQQ", "ProShares UltraPro Short QQQ"),
                _us_mapped_row("PSQ", "ProShares Short QQQ"),
                _us_mapped_row("NVDQ", "T-Rex 2X Inverse NVIDIA Daily"),
                _us_mapped_row("VOO", "Vanguard S&P 500 ETF"),
                _us_mapped_row("ULTA", "Ulta Beauty Inc."),
                _us_mapped_row("SGOV", "iShares 0-3 Month Treasury Bond ETF"),
            ],
        )

        assert [row["symbol"] for row in result["rankings"]] == [
            "VOO",
            "ULTA",
            "SGOV",
        ]
        assert result["instrument_filter"]["excluded_leveraged_inverse_etf_count"] == 4

    async def test_us_short_duration_names_not_in_scope(self, monkeypatch):
        """Duration names are not leveraged/inverse products."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("SHV", "iShares Short Treasury Bond ETF"),
                _us_mapped_row("VUSB", "Vanguard Ultra-Short Bond ETF"),
                _us_mapped_row("MINT", "PIMCO Short-Term Active ETF"),
                _us_mapped_row("LONG", "Long Duration Co", market_cap=5e9),
            ],
        )

        assert [row["symbol"] for row in result["rankings"]] == [
            "SHV",
            "VUSB",
            "MINT",
            "LONG",
        ]
        assert result["instrument_filter"]["excluded_leveraged_inverse_etf_count"] == 0

    async def test_us_include_illiquid_bypasses_the_default_bar(self, monkeypatch):
        """The escape hatch returns the raw list — no floors, no ETF exclusion."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("NOCAP", "No Cap Co", market_cap=None),
                _us_mapped_row("SOXL", "Direxion Daily Semiconductor Bull 3X Shares"),
            ],
            include_illiquid=True,
        )

        assert [row["symbol"] for row in result["rankings"]] == [
            "NOCAP",
            "SOXL",
        ]
        assert "market_cap_filter" not in result
        assert "turnover_filter" not in result
        assert "instrument_filter" not in result

    async def test_us_explicit_floors_still_apply_under_include_illiquid(
        self, monkeypatch
    ):
        """Like the KR path, an explicit caller floor is never bypassed."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row(
                    "SOXL",
                    "Direxion Daily Semiconductor Bull 3X Shares",
                    market_cap=5e9,
                ),
                _us_mapped_row("SMALL", "Small Cap Co", market_cap=1e9),
            ],
            include_illiquid=True,
            min_market_cap=2_000_000_000,
        )

        assert [row["symbol"] for row in result["rankings"]] == ["SOXL"]
        assert result["market_cap_filter"]["excluded_count"] == 1
        assert "instrument_filter" not in result
        assert "turnover_filter" not in result

    async def test_us_explicit_floor_overrides_settings_default(self, monkeypatch):
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("MID", "Mid Cap Co", market_cap=5e9),
                _us_mapped_row("LOW", "Low Cap Co", market_cap=1e9),
            ],
            min_market_cap=4_000_000_000,
        )

        assert [row["symbol"] for row in result["rankings"]] == ["MID"]
        assert result["market_cap_filter"]["min_market_cap"] == 4_000_000_000

    async def test_us_settings_defaults_are_operator_tunable(self, monkeypatch):
        monkeypatch.setattr(
            analysis_tool_handlers.settings,
            "us_top_stocks_min_market_cap",
            10_000_000_000.0,
        )

        result = await self._run(
            monkeypatch,
            [_us_mapped_row("MID", "Mid Cap Co", market_cap=5e9)],
        )

        assert result["rankings"] == []
        assert result["status"] == "degraded"
        assert result["market_cap_filter"]["min_market_cap"] == 10_000_000_000.0

    async def test_us_gainers_apply_the_same_default_bar(self, monkeypatch):
        """The quality bar is uniform across US ranking types, not losers-only."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("SMALL", "Small Cap Co", market_cap=1e9),
                _us_mapped_row("BIG", "Big Cap Co", market_cap=3e12),
            ],
            ranking_type="gainers",
        )

        assert [row["symbol"] for row in result["rankings"]] == ["BIG"]
        assert result["market_cap_filter"]["excluded_count"] == 1

    async def test_us_filter_emptied_list_is_degraded_not_fake(self, monkeypatch):
        """A filter-emptied list is an honest degraded response, KR-mirrored."""
        result = await self._run(
            monkeypatch,
            [
                _us_mapped_row("NOCAP", "No Cap Co", market_cap=None),
                _us_mapped_row("SMALL", "Small Cap Co", market_cap=1e9),
            ],
        )

        assert result["status"] == "degraded"
        assert result["rankings"] == []
        assert result["total_count"] == 0
        assert "US quality bar" in result["degraded_reason"]
        assert result["market_cap_filter"]["excluded_count"] == 2
        assert result["market_cap_filter"]["missing_market_cap_excluded_count"] == 1
        assert result["instrument_filter"] == {
            "excluded_leveraged_inverse_etf_count": 0
        }

    async def test_us_end_to_end_through_yf_screen_and_map(self, monkeypatch):
        """Raw yfinance quotes map through _map_us_row then the quality bar."""
        tools = build_tools()

        import pandas as pd

        mock_df = pd.DataFrame(
            {
                "symbol": ["SOXL", "AAPL", "NOCAP"],
                "longName": [
                    "Direxion Daily Semiconductor Bull 3X Shares",
                    "Apple Inc.",
                    "No Cap Co",
                ],
                "regularMarketPrice": [10.0, 200.0, 1.0],
                "previousClose": [12.0, 205.0, 1.1],
                "regularMarketVolume": [50_000_000, 10_000_000, 100],
                "marketCap": [5e9, 3e12, None],
            }
        )

        def mock_screen(*args, **kwargs):
            return mock_df

        monkeypatch.setattr(yf, "screen", mock_screen)

        result = await tools["get_top_stocks"](
            market="us", ranking_type="losers", limit=5
        )

        # SOXL excluded as leveraged; NOCAP fails the cap floor; AAPL survives.
        assert [row["symbol"] for row in result["rankings"]] == ["AAPL"]
        assert result["instrument_filter"]["excluded_leveraged_inverse_etf_count"] == 1
        assert result["market_cap_filter"]["missing_market_cap_excluded_count"] == 1


class TestUSLeveragedInverseNames:
    """Token-level contract for the KR-mirrored US name exclusion."""

    @pytest.mark.parametrize(
        "name",
        [
            "Direxion Daily S&P 500 Bull 3X Shares",
            "Direxion Daily Semiconductor Bear 3X Shares",
            "ProShares UltraPro QQQ",
            "ProShares UltraShort S&P500",
            "ProShares Short QQQ",
            "ProShares Ultra S&P500",
            "GraniteShares 2x Long NVDA Daily ETF",
            "T-Rex 2X Inverse MSTR Daily Target ETF",
            "Tuttle Capital Short Innovation ETF",
            "1.5X Long Something Daily Fund",
        ],
    )
    def test_leveraged_inverse_names_excluded(self, name: str) -> None:
        from app.mcp_server.tooling.screening.instrument_type import (
            is_us_leveraged_inverse_name,
        )

        assert is_us_leveraged_inverse_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Vanguard S&P 500 ETF",
            "iShares Core S&P 500 ETF",
            "Apple Inc.",
            "Ulta Beauty Inc.",
            "iShares Short Treasury Bond ETF",
            "Vanguard Ultra-Short Bond ETF",
            "PIMCO Short-Term Active ETF",
            "Long Duration Co",
            "",
            None,
        ],
    )
    def test_ordinary_names_not_excluded(self, name) -> None:
        from app.mcp_server.tooling.screening.instrument_type import (
            is_us_leveraged_inverse_name,
        )

        assert is_us_leveraged_inverse_name(name) is False
