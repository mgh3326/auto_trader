"""Tests for recommend_stocks MCP tool and related helpers."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from app.mcp_server import tools as mcp_tools
from app.mcp_server.scoring import (
    calc_composite_score,
    calc_dividend_score,
    calc_momentum_score,
    calc_rsi_score,
    calc_valuation_score,
)
from app.mcp_server.strategies import (
    VALID_STRATEGIES,
    get_strategy_config,
    validate_account,
    validate_strategy,
)


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
    mcp_tools.register_tools(mcp)
    return mcp.tools


def _mock_kr_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stk: list[dict[str, Any]],
    ksq: list[dict[str, Any]] | None = None,
    etfs: list[dict[str, Any]] | None = None,
    valuations: dict[str, dict[str, Any]] | None = None,
) -> None:
    async def mock_fetch_stock_all_cached(market: str) -> list[dict[str, Any]]:
        if market == "STK":
            return [dict(item) for item in stk]
        if market == "KSQ":
            return [dict(item) for item in (ksq or [])]
        return []

    async def mock_fetch_etf_all_cached() -> list[dict[str, Any]]:
        return [dict(item) for item in (etfs or [])]

    async def mock_fetch_valuation_all_cached(
        market: str,
    ) -> dict[str, dict[str, Any]]:
        return valuations or {}

    monkeypatch.setattr(mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
    monkeypatch.setattr(mcp_tools, "fetch_etf_all_cached", mock_fetch_etf_all_cached)
    monkeypatch.setattr(
        mcp_tools,
        "fetch_valuation_all_cached",
        mock_fetch_valuation_all_cached,
    )


def _mock_empty_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_collect_portfolio_positions(
        *,
        account: str | None,
        market: str | None,
        include_current_price: bool,
        user_id: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
        return [], [], market, account

    monkeypatch.setattr(
        mcp_tools,
        "_collect_portfolio_positions",
        mock_collect_portfolio_positions,
    )


class TestScoringFunctions:
    def test_calc_rsi_score_handles_none(self):
        assert calc_rsi_score(None) == 50.0

    def test_calc_valuation_score_handles_none(self):
        assert calc_valuation_score(None, None) == 50.0

    def test_calc_momentum_score_handles_none(self):
        assert calc_momentum_score(None) == 50.0

    def test_calc_dividend_score_accepts_percent_input(self):
        score_decimal = calc_dividend_score(0.05)
        score_percent = calc_dividend_score(5.0)
        assert score_decimal == score_percent

    def test_calc_composite_score_range(self):
        score = calc_composite_score(
            {
                "rsi_14": 42,
                "per": 12,
                "pbr": 1.2,
                "change_rate": 2.1,
                "volume": 10_000_000,
                "dividend_yield": 0.03,
            }
        )
        assert 0 <= score <= 100


class TestStrategyAndAccountValidation:
    def test_validate_strategy_values(self):
        for strategy in VALID_STRATEGIES:
            assert validate_strategy(strategy) == strategy

    def test_validate_strategy_invalid(self):
        with pytest.raises(ValueError, match="Invalid strategy"):
            validate_strategy("invalid")

    def test_strategy_config_schema(self):
        config = get_strategy_config("balanced")
        assert set(config.keys()) == {"description", "screen_params", "scoring_weights"}
        assert "sort_by" in config["screen_params"]
        assert "rsi_weight" in config["scoring_weights"]

    def test_isa_requires_asset_type_even_when_none(self):
        with pytest.raises(ValueError, match="requires asset_type"):
            validate_account("isa", "kr", None)

    def test_samsung_pension_requires_asset_type_even_when_none(self):
        with pytest.raises(ValueError, match="requires asset_type"):
            validate_account("samsung_pension", "kr", None)


class TestBudgetAllocation:
    @pytest.fixture
    def allocate_budget(self):
        build_tools()
        return mcp_tools._allocate_budget

    def test_score_proportional_allocation(self, allocate_budget):
        candidates = [
            {"symbol": "A", "price": 100, "score": 90},
            {"symbol": "B", "price": 100, "score": 10},
        ]
        allocated, remaining = allocate_budget(candidates, 1000, 2)
        assert len(allocated) == 2
        quantities = {item["symbol"]: item["quantity"] for item in allocated}
        assert quantities["A"] > quantities["B"]
        assert remaining == 0

    def test_allocation_dedupes_symbols(self, allocate_budget):
        candidates = [
            {"symbol": "AAA", "price": 1000, "score": 80},
            {"symbol": "AAA", "price": 1000, "score": 70},
            {"symbol": "BBB", "price": 1000, "score": 60},
        ]
        allocated, _ = allocate_budget(candidates, 10_000, 5)
        symbols = [item["symbol"] for item in allocated]
        assert symbols.count("AAA") == 1

    def test_allocation_insufficient_budget(self, allocate_budget):
        candidates = [{"symbol": "A", "price": 200_000, "score": 80}]
        allocated, remaining = allocate_budget(candidates, 50_000, 1)
        assert allocated == []
        assert remaining == 50_000


class TestCandidateNormalization:
    @pytest.fixture
    def normalize_candidate(self):
        build_tools()
        return mcp_tools._normalize_candidate

    def test_symbol_priority_symbol_first(self, normalize_candidate):
        item = {
            "symbol": "KRW-BTC",
            "code": "005930",
            "original_market": "KRW-ETH",
            "market": "crypto",
            "trade_price": 1000,
        }
        normalized = normalize_candidate(item, "crypto")
        assert normalized["symbol"] == "KRW-BTC"

    def test_symbol_priority_fallback_original_market(self, normalize_candidate):
        item = {"original_market": "KRW-XRP", "market": "crypto", "trade_price": 900}
        normalized = normalize_candidate(item, "crypto")
        assert normalized["symbol"] == "KRW-XRP"


class TestRecommendStocksIntegration:
    @pytest.fixture
    def recommend_stocks(self):
        return build_tools()["recommend_stocks"]

    @pytest.mark.asyncio
    async def test_rejects_unsupported_market(self, recommend_stocks):
        with pytest.raises(ValueError, match="market must be one of"):
            await recommend_stocks(budget=100_000, market="jp")

    @pytest.mark.asyncio
    async def test_max_positions_upper_bound(self, recommend_stocks):
        with pytest.raises(ValueError, match="between 1 and 20"):
            await recommend_stocks(budget=100_000, max_positions=21)

    @pytest.mark.asyncio
    async def test_isa_account_requires_asset_type(self, recommend_stocks):
        with pytest.raises(ValueError, match="requires asset_type"):
            await recommend_stocks(
                budget=300_000,
                market="kr",
                account="isa",
                strategy="balanced",
            )

    @pytest.mark.asyncio
    async def test_kr_success_path_and_warnings_is_list(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "005930",
                    "name": "삼성전자",
                    "close": 80_000,
                    "volume": 1_000_000,
                    "change_rate": 1.2,
                    "market_cap": 1000,
                },
                {
                    "code": "000660",
                    "name": "SK하이닉스",
                    "close": 120_000,
                    "volume": 900_000,
                    "change_rate": 0.7,
                    "market_cap": 1200,
                },
            ],
            valuations={
                "005930": {"per": 12.0, "pbr": 1.2, "dividend_yield": 0.02},
                "000660": {"per": 14.0, "pbr": 1.4, "dividend_yield": 0.015},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=1_000_000,
            market="kr",
            strategy="balanced",
            max_positions=2,
        )

        assert result["recommendations"]
        assert isinstance(result["warnings"], list)
        assert result["warnings"] == []

    @pytest.mark.asyncio
    async def test_strategy_specific_filter_value_vs_balanced(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "111111",
                    "name": "저PER",
                    "close": 10_000,
                    "volume": 1_000_000,
                    "change_rate": 1.0,
                    "market_cap": 2000,
                },
                {
                    "code": "222222",
                    "name": "고PER",
                    "close": 10_000,
                    "volume": 1_000_000,
                    "change_rate": 1.0,
                    "market_cap": 2000,
                },
            ],
            valuations={
                "111111": {"per": 10.0, "pbr": 1.0, "dividend_yield": 0.02},
                "222222": {"per": 30.0, "pbr": 1.0, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        balanced_result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="balanced",
            max_positions=2,
        )
        value_result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="value",
            max_positions=2,
        )

        balanced_symbols = {item["symbol"] for item in balanced_result["recommendations"]}
        value_symbols = {item["symbol"] for item in value_result["recommendations"]}
        assert "111111" in balanced_symbols
        assert "222222" in balanced_symbols
        assert value_symbols == {"111111"}

    @pytest.mark.asyncio
    async def test_strategy_specific_filter_dividend(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "333333",
                    "name": "저배당",
                    "close": 10_000,
                    "volume": 500_000,
                    "change_rate": 0.5,
                    "market_cap": 1200,
                },
                {
                    "code": "444444",
                    "name": "고배당",
                    "close": 10_000,
                    "volume": 500_000,
                    "change_rate": 0.5,
                    "market_cap": 1200,
                },
            ],
            valuations={
                "333333": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.01},
                "444444": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.04},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="dividend",
            max_positions=2,
        )
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert symbols == {"444444"}

    @pytest.mark.asyncio
    async def test_holdings_auto_exclusion_account_none(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "555555",
                    "name": "보유종목",
                    "close": 10_000,
                    "volume": 700_000,
                    "change_rate": 1.0,
                    "market_cap": 1500,
                },
                {
                    "code": "666666",
                    "name": "후보종목",
                    "close": 10_000,
                    "volume": 700_000,
                    "change_rate": 1.0,
                    "market_cap": 1500,
                },
            ],
            valuations={
                "555555": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.02},
                "666666": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.02},
            },
        )

        captured: dict[str, Any] = {}

        async def mock_collect_portfolio_positions(
            *,
            account: str | None,
            market: str | None,
            include_current_price: bool,
            user_id: int,
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
            captured["account"] = account
            return [{"symbol": "555555"}], [], market, account

        monkeypatch.setattr(
            mcp_tools,
            "_collect_portfolio_positions",
            mock_collect_portfolio_positions,
        )

        result = await recommend_stocks(
            budget=300_000,
            market="kr",
            strategy="balanced",
            max_positions=2,
        )
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert captured["account"] is None
        assert "555555" not in symbols
        assert "666666" in symbols

    @pytest.mark.asyncio
    async def test_holdings_auto_exclusion_account_specific(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "777777",
                    "name": "보유종목",
                    "close": 10_000,
                    "volume": 700_000,
                    "change_rate": 1.0,
                    "market_cap": 1500,
                },
                {
                    "code": "888888",
                    "name": "후보종목",
                    "close": 10_000,
                    "volume": 700_000,
                    "change_rate": 1.0,
                    "market_cap": 1500,
                },
            ],
            valuations={
                "777777": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.02},
                "888888": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.02},
            },
        )

        captured: dict[str, Any] = {}

        async def mock_collect_portfolio_positions(
            *,
            account: str | None,
            market: str | None,
            include_current_price: bool,
            user_id: int,
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
            captured["account"] = account
            return [{"symbol": "777777"}], [], market, account

        monkeypatch.setattr(
            mcp_tools,
            "_collect_portfolio_positions",
            mock_collect_portfolio_positions,
        )

        result = await recommend_stocks(
            budget=300_000,
            market="kr",
            account="kis",
            asset_type="stock",
            strategy="balanced",
            max_positions=2,
        )
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert captured["account"] == "kis"
        assert "777777" not in symbols
        assert "888888" in symbols

    @pytest.mark.asyncio
    async def test_us_success_path(self, recommend_stocks, monkeypatch: pytest.MonkeyPatch):
        _mock_empty_holdings(monkeypatch)

        def mock_yf_screen(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "shortname": "Apple",
                        "lastprice": 200.0,
                        "percentchange": 1.1,
                        "dayvolume": 10_000_000,
                        "intradaymarketcap": 3_000_000_000_000,
                        "peratio": 25.0,
                        "forward_dividend_yield": 0.006,
                    },
                    {
                        "symbol": "MSFT",
                        "shortname": "Microsoft",
                        "lastprice": 400.0,
                        "percentchange": 0.8,
                        "dayvolume": 8_000_000,
                        "intradaymarketcap": 2_500_000_000_000,
                        "peratio": 30.0,
                        "forward_dividend_yield": 0.007,
                    },
                ],
                "total": 2,
            }

        monkeypatch.setattr(mcp_tools.yf, "screen", mock_yf_screen)

        # Keep import path stable even if yfinance.screener is unavailable in env.
        class DummyEquityQuery:
            def __init__(self, *_args: Any, **_kwargs: Any):
                pass

        screener_module = types.SimpleNamespace(EquityQuery=DummyEquityQuery)
        monkeypatch.setitem(sys.modules, "yfinance.screener", screener_module)

        result = await recommend_stocks(
            budget=1_000,
            market="us",
            strategy="growth",
            max_positions=2,
        )

        assert result["recommendations"]
        assert all(item["symbol"] in {"AAPL", "MSFT"} for item in result["recommendations"])
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_crypto_success_symbol_preserved(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_fetch_top_traded_coins(fiat: str = "KRW") -> list[dict[str, Any]]:
            assert fiat == "KRW"
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_price_24h": 1_000_000_000_000,
                    "volume": 10_000,
                },
                {
                    "market": "KRW-ETH",
                    "korean_name": "이더리움",
                    "trade_price": 5_000_000,
                    "signed_change_rate": 0.02,
                    "acc_trade_price_24h": 800_000_000_000,
                    "volume": 20_000,
                },
            ]

        monkeypatch.setattr(
            mcp_tools.upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await recommend_stocks(
            budget=10_000_000,
            market="crypto",
            strategy="momentum",
            max_positions=2,
        )

        assert result["recommendations"]
        symbols = [item["symbol"] for item in result["recommendations"]]
        assert all(symbol.startswith("KRW-") for symbol in symbols)
        assert "crypto" not in symbols
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_crypto_unsupported_strategy_filters_add_warnings(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_fetch_top_traded_coins(fiat: str = "KRW") -> list[dict[str, Any]]:
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_price_24h": 1_000_000_000_000,
                    "volume": 10_000,
                }
            ]

        monkeypatch.setattr(
            mcp_tools.upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        result = await recommend_stocks(
            budget=1_000_000,
            market="crypto",
            strategy="dividend",
            max_positions=1,
        )

        assert result["recommendations"]
        assert any("dividend_yield" in warning for warning in result["warnings"])
