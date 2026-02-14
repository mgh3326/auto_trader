from unittest.mock import MagicMock

import pytest

from app.mcp_server.tooling import analysis_tool_handlers
from app.mcp_server.tooling.registry import register_all_tools
from app.services import upbit as upbit_service
import yfinance as yf


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


@pytest.mark.asyncio
class TestMCPTopStocks:
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
        assert result["rankings"][0]["change_rate"] == 2.5
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
        assert result["rankings"][0]["change_rate"] == 5.0

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
            async def foreign_buying_rank(self, market, limit):
                return [
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.0",
                        "acml_vol": "20000000",
                        "hts_avls": "5000000000000",
                        "acml_tr_pbmn": "700000000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"

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
        assert result["rankings"][0]["change_rate"] == 5.0

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
        assert result["rankings"][0]["change_rate"] == -3.0

    async def test_kr_foreigners_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit):
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

        monkeypatch.setattr(yf, "screen", lambda sid: mock_df)

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
        monkeypatch.setattr(yf, "screen", lambda *args, **kw: mock_df)

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
        assert result["rankings"][0]["change_rate"] == 5.0

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
        assert result["rankings"][0]["change_rate"] == -1.0

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

        assert result["rankings"][0]["change_rate"] == 2.5

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
        """foreigners 랭킹에서 외국인 전용 필드(frgn_ntby_qty, frgn_ntby_tr_pbmn) 사용 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "hts_avls": "100000000000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "1.5",
                        "frgn_ntby_qty": "3000000",
                        "hts_avls": "50000000000000",
                        "frgn_ntby_tr_pbmn": "360000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 2

        assert result["rankings"][0]["symbol"] == "005930"
        assert result["rankings"][0]["name"] == "삼성전자"
        assert result["rankings"][0]["volume"] == 5000000
        assert result["rankings"][0]["trade_amount"] == 400000000000.0

        assert result["rankings"][1]["symbol"] == "005380"
        assert result["rankings"][1]["name"] == "LG전자"
        assert result["rankings"][1]["volume"] == 3000000
        assert result["rankings"][1]["trade_amount"] == 360000000000.0


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
        assert float(result["rankings"][0]["change_rate"]) == -3.0
        assert float(result["rankings"][1]["change_rate"]) == -2.0

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
        assert float(result["rankings"][0]["change_rate"]) == 5.0

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

        monkeypatch.setattr(yf, "screen", lambda sid: mock_df)

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
        assert result["rankings"][0]["change_rate"] == -2.0  # -0.02 * 100
        assert result["rankings"][1]["symbol"] == "KRW-BTC"
        assert result["rankings"][1]["change_rate"] == -1.0  # -0.01 * 100
