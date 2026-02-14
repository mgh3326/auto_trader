"""Tests for recommend_stocks MCP tool and related helpers."""

from __future__ import annotations

from typing import Any

import pytest

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
    validate_strategy,
)
from app.mcp_server.tooling import analysis_tool_handlers
from app.mcp_server.tooling.testing_proxy import mcp_tools


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

    monkeypatch.setattr(
        mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached
    )
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


class TestStrategyValidation:
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
    async def test_rejects_removed_asset_type_parameter(self, recommend_stocks):
        with pytest.raises(TypeError, match="asset_type"):
            await recommend_stocks(
                budget=300_000,
                market="kr",
                strategy="balanced",
                asset_type="stock",  # type: ignore[call-arg]
            )

    @pytest.mark.asyncio
    async def test_rejects_removed_account_parameter(self, recommend_stocks):
        with pytest.raises(TypeError, match="account"):
            await recommend_stocks(
                budget=300_000,
                market="kr",
                strategy="balanced",
                account="kis",  # type: ignore[call-arg]
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

        balanced_symbols = {
            item["symbol"] for item in balanced_result["recommendations"]
        }
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
                "333333": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.005},
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
    async def test_us_success_path(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_get_us_rankings(
            ranking_type: str, limit: int
        ) -> tuple[list[dict[str, Any]], str]:
            return [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "price": 200.0,
                    "change_rate": 1.1,
                    "volume": 10_000_000,
                    "market_cap": 3_000_000_000_000,
                    "rank": 1,
                },
                {
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "price": 400.0,
                    "change_rate": 0.8,
                    "volume": 8_000_000,
                    "market_cap": 2_500_000_000_000,
                    "rank": 2,
                },
            ], "yfinance"

        monkeypatch.setattr(
            mcp_tools,
            "_get_us_rankings",
            mock_get_us_rankings,
        )

        result = await recommend_stocks(
            budget=1_000,
            market="us",
            strategy="growth",
            max_positions=2,
        )

        assert result["recommendations"]
        assert all(
            item["symbol"] in {"AAPL", "MSFT"} for item in result["recommendations"]
        )
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_us_top_stocks_exception_returns_empty_with_warning(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_get_top_stocks_raises(
            *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            raise RuntimeError("US source timeout")

        monkeypatch.setattr(
            analysis_tool_handlers,
            "get_top_stocks_impl",
            mock_get_top_stocks_raises,
        )

        result = await recommend_stocks(
            budget=2_000,
            market="us",
            strategy="growth",
            max_positions=2,
        )

        assert result["recommendations"] == []
        assert result["total_amount"] == 0
        assert result["remaining_budget"] == 2_000
        assert any("US 후보 수집 실패" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_rsi_enrichment_populates_missing_rsi(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "777777",
                    "name": "RSI테스트1",
                    "close": 10_000,
                    "volume": 1_500_000,
                    "change_rate": 1.0,
                    "market_cap": 1500,
                },
                {
                    "code": "888888",
                    "name": "RSI테스트2",
                    "close": 12_000,
                    "volume": 1_700_000,
                    "change_rate": 0.8,
                    "market_cap": 1700,
                },
            ],
            valuations={
                "777777": {"per": 11.0, "pbr": 1.0, "dividend_yield": 0.02},
                "888888": {"per": 12.0, "pbr": 1.1, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        async def mock_get_indicators_impl(
            symbol: str, indicators: list[str], market: str | None = None
        ) -> dict[str, Any]:
            assert indicators == ["rsi"]
            return {
                "symbol": symbol,
                "instrument_type": "equity_kr",
                "source": "mock",
                "indicators": {"rsi": {"14": 37.5}},
            }

        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_get_indicators_impl)

        result = await recommend_stocks(
            budget=1_000_000,
            market="kr",
            strategy="balanced",
            max_positions=2,
        )

        assert result["recommendations"]
        assert all(rec["rsi_14"] is not None for rec in result["recommendations"])

    @pytest.mark.asyncio
    async def test_reason_includes_rich_context(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "999999",
                    "name": "리즌테스트",
                    "close": 15_000,
                    "volume": 12_000_000,
                    "change_rate": 6.0,
                    "market_cap": 2000,
                }
            ],
            valuations={
                "999999": {"per": 7.5, "pbr": 0.9, "dividend_yield": 0.05},
            },
        )
        _mock_empty_holdings(monkeypatch)

        async def mock_get_indicators_impl(
            symbol: str, indicators: list[str], market: str | None = None
        ) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "instrument_type": "equity_kr",
                "source": "mock",
                "indicators": {"rsi": {"14": 28.0}},
            }

        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_get_indicators_impl)

        result = await recommend_stocks(
            budget=300_000,
            market="kr",
            strategy="balanced",
            max_positions=1,
        )

        assert result["recommendations"]
        reason = result["recommendations"][0]["reason"]
        assert reason.startswith("[balanced]")
        assert "RSI" in reason
        assert "PER" in reason
        assert "거래량" in reason or "모멘텀" in reason or "배당" in reason

    @pytest.mark.asyncio
    async def test_crypto_success_symbol_preserved(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_fetch_top_traded_coins(
            fiat: str = "KRW",
        ) -> list[dict[str, Any]]:
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

        async def mock_fetch_top_traded_coins(
            fiat: str = "KRW",
        ) -> list[dict[str, Any]]:
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

    @pytest.mark.asyncio
    async def test_zero_candidates_empty_result(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[],
            valuations={},
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=1_000_000,
            market="kr",
            strategy="balanced",
            max_positions=5,
        )

        assert result["recommendations"] == []
        assert result["total_amount"] == 0
        assert result["remaining_budget"] == 1_000_000
        assert result["candidates_screened"] == 0
        assert isinstance(result["warnings"], list)
        assert "disclaimer" in result

    @pytest.mark.asyncio
    async def test_all_candidates_excluded(
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
            exclude_symbols=["005930", "000660"],
            max_positions=5,
        )

        assert result["recommendations"] == []
        assert result["total_amount"] == 0
        assert result["remaining_budget"] == 1_000_000
        assert result["candidates_screened"] == 2
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_disclaimer_present(
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
            ],
            valuations={
                "005930": {"per": 12.0, "pbr": 1.2, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=300_000,
            market="kr",
            strategy="balanced",
            max_positions=1,
        )

        assert "disclaimer" in result
        assert isinstance(result["disclaimer"], str)
        assert len(result["disclaimer"]) > 0
        assert (
            "투자" in result["disclaimer"]
            or "investment" in result["disclaimer"].lower()
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "strategy",
        ["balanced", "growth", "value", "dividend", "momentum"],
    )
    async def test_all_strategies_return_valid_payload(
        self,
        recommend_stocks,
        monkeypatch: pytest.MonkeyPatch,
        strategy: str,
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "101010",
                    "name": "전략테스트1",
                    "close": 10_000,
                    "volume": 1_500_000,
                    "change_rate": 2.2,
                    "market_cap": 2_000,
                },
                {
                    "code": "202020",
                    "name": "전략테스트2",
                    "close": 12_000,
                    "volume": 1_200_000,
                    "change_rate": 1.5,
                    "market_cap": 2_500,
                },
                {
                    "code": "303030",
                    "name": "전략테스트3",
                    "close": 15_000,
                    "volume": 900_000,
                    "change_rate": 0.7,
                    "market_cap": 1_800,
                },
            ],
            valuations={
                "101010": {"per": 10.0, "pbr": 1.1, "dividend_yield": 0.03},
                "202020": {"per": 15.0, "pbr": 1.5, "dividend_yield": 0.025},
                "303030": {"per": 8.0, "pbr": 0.9, "dividend_yield": 0.04},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=1_000_000,
            market="kr",
            strategy=strategy,
            max_positions=3,
        )

        assert result["strategy"] == strategy
        assert "strategy_description" in result
        assert isinstance(result["warnings"], list)
        assert result["total_amount"] <= 1_000_000
        assert len(result["recommendations"]) <= 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("budget", "max_positions"),
        [
            (300_000, 1),
            (750_000, 2),
            (2_000_000, 4),
        ],
    )
    async def test_budget_and_position_matrix(
        self,
        recommend_stocks,
        monkeypatch: pytest.MonkeyPatch,
        budget: int,
        max_positions: int,
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "121212",
                    "name": "예산테스트1",
                    "close": 50_000,
                    "volume": 1_300_000,
                    "change_rate": 1.0,
                    "market_cap": 1_500,
                },
                {
                    "code": "131313",
                    "name": "예산테스트2",
                    "close": 70_000,
                    "volume": 1_200_000,
                    "change_rate": 1.3,
                    "market_cap": 1_700,
                },
                {
                    "code": "141414",
                    "name": "예산테스트3",
                    "close": 90_000,
                    "volume": 1_100_000,
                    "change_rate": 1.7,
                    "market_cap": 1_900,
                },
                {
                    "code": "151515",
                    "name": "예산테스트4",
                    "close": 110_000,
                    "volume": 1_000_000,
                    "change_rate": 2.0,
                    "market_cap": 2_100,
                },
            ],
            valuations={
                "121212": {"per": 11.0, "pbr": 1.2, "dividend_yield": 0.02},
                "131313": {"per": 13.0, "pbr": 1.3, "dividend_yield": 0.018},
                "141414": {"per": 9.0, "pbr": 1.1, "dividend_yield": 0.03},
                "151515": {"per": 16.0, "pbr": 1.6, "dividend_yield": 0.015},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=budget,
            market="kr",
            strategy="balanced",
            max_positions=max_positions,
        )

        assert result["total_amount"] <= budget
        assert result["remaining_budget"] >= 0
        assert len(result["recommendations"]) <= max_positions

    @pytest.mark.asyncio
    async def test_handles_missing_valuation_fields(
        self, recommend_stocks, monkeypatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "161616",
                    "name": "밸류결측치테스트",
                    "close": 8_000,
                    "volume": 1_000_000,
                    "change_rate": 0.8,
                    "market_cap": 1_200,
                }
            ],
            valuations={},
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=200_000,
            market="kr",
            strategy="balanced",
            max_positions=1,
        )

        assert result["recommendations"]
        rec = result["recommendations"][0]
        assert rec["per"] is None
        assert rec["symbol"] == "161616"

    @pytest.mark.asyncio
    async def test_budget_below_minimum_purchase_adds_warning(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "171717",
                    "name": "최소매수테스트",
                    "close": 250_000,
                    "volume": 1_000_000,
                    "change_rate": 1.1,
                    "market_cap": 1_500,
                }
            ],
            valuations={
                "171717": {"per": 12.0, "pbr": 1.2, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=100_000,
            market="kr",
            strategy="balanced",
            max_positions=1,
        )

        assert result["recommendations"] == []
        assert result["remaining_budget"] == 100_000
        assert any("최소 구매 금액" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_exclude_symbols_handles_case_and_whitespace(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_empty_holdings(monkeypatch)

        async def mock_get_top_stocks(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "rankings": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple",
                        "price": 200.0,
                        "change_rate": 1.1,
                        "volume": 10_000_000,
                        "market_cap": 3_000_000_000_000,
                    },
                    {
                        "symbol": "MSFT",
                        "name": "Microsoft",
                        "price": 400.0,
                        "change_rate": 0.8,
                        "volume": 8_000_000,
                        "market_cap": 2_500_000_000_000,
                    },
                ]
            }

        monkeypatch.setattr(
            analysis_tool_handlers,
            "get_top_stocks_impl",
            mock_get_top_stocks,
        )

        result = await recommend_stocks(
            budget=2_000,
            market="us",
            strategy="growth",
            exclude_symbols=[" aapl "],
            max_positions=2,
        )

        symbols = {item["symbol"] for item in result["recommendations"]}
        assert "AAPL" not in symbols
        assert "MSFT" in symbols

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_traceback_payload(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        async def mock_fetch_stock_all_cached(market: str) -> list[dict[str, Any]]:
            raise RuntimeError()

        monkeypatch.setattr(
            mcp_tools,
            "fetch_stock_all_cached",
            mock_fetch_stock_all_cached,
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="balanced",
            max_positions=5,
        )

        assert result["source"] == "recommend_stocks"
        assert result["error"].startswith("recommend_stocks failed:")
        assert "RuntimeError" in result["error"]
        assert "details" in result
        assert "Traceback" in result["details"]


class TestTwoStageRelaxation:
    """Test 2-stage relaxation for value/dividend strategies."""

    @pytest.fixture
    def recommend_stocks(self):
        return build_tools()["recommend_stocks"]

    @pytest.mark.asyncio
    async def test_value_fallback_triggered(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "111111",
                    "name": "엄격필터통과",
                    "close": 10_000,
                    "volume": 1_000_000,
                    "change_rate": 1.0,
                    "market_cap": 500,
                },
                {
                    "code": "222222",
                    "name": "완화필터통과",
                    "close": 15_000,
                    "volume": 800_000,
                    "change_rate": 0.8,
                    "market_cap": 250,
                },
            ],
            valuations={
                "111111": {"per": 15.0, "pbr": 1.0, "dividend_yield": 0.02},
                "222222": {"per": 22.0, "pbr": 1.8, "dividend_yield": 0.01},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="value",
            max_positions=3,
        )

        assert result["fallback_applied"] is True
        assert result["diagnostics"]["fallback_candidates_added"] >= 1
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert "111111" in symbols
        assert "222222" in symbols

    @pytest.mark.asyncio
    async def test_dividend_fallback_triggered(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "333333",
                    "name": "고배당",
                    "close": 10_000,
                    "volume": 500_000,
                    "change_rate": 0.5,
                    "market_cap": 500,
                },
                {
                    "code": "444444",
                    "name": "중간배당",
                    "close": 12_000,
                    "volume": 400_000,
                    "change_rate": 0.3,
                    "market_cap": 250,
                },
            ],
            valuations={
                "333333": {"per": 12.0, "pbr": 1.0, "dividend_yield": 0.04},
                "444444": {"per": 10.0, "pbr": 0.8, "dividend_yield": 0.012},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="dividend",
            max_positions=3,
        )

        assert result["fallback_applied"] is True
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert "333333" in symbols
        assert "444444" in symbols

    @pytest.mark.asyncio
    async def test_diagnostics_fields_present(
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
            ],
            valuations={
                "005930": {"per": 12.0, "pbr": 1.2, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="balanced",
            max_positions=1,
        )

        assert "diagnostics" in result
        diagnostics = result["diagnostics"]
        assert "raw_candidates" in diagnostics
        assert "post_filter_candidates" in diagnostics
        assert "strict_candidates" in diagnostics
        assert "fallback_applied" in diagnostics
        assert "per_none_count" in diagnostics
        assert "pbr_none_count" in diagnostics
        assert "dividend_none_count" in diagnostics
        assert "active_thresholds" in diagnostics
        assert isinstance(result["fallback_applied"], bool)

    @pytest.mark.asyncio
    async def test_dividend_excludes_missing_dividend_yield_from_fallback(
        self, recommend_stocks, monkeypatch: pytest.MonkeyPatch
    ):
        _mock_kr_sources(
            monkeypatch,
            stk=[
                {
                    "code": "555555",
                    "name": "배당없음",
                    "close": 10_000,
                    "volume": 500_000,
                    "change_rate": 0.5,
                    "market_cap": 250,
                },
                {
                    "code": "666666",
                    "name": "배당있음",
                    "close": 12_000,
                    "volume": 400_000,
                    "change_rate": 0.3,
                    "market_cap": 260,
                },
            ],
            valuations={
                "555555": {"per": 10.0, "pbr": 0.8, "dividend_yield": None},
                "666666": {"per": 11.0, "pbr": 0.9, "dividend_yield": 0.02},
            },
        )
        _mock_empty_holdings(monkeypatch)

        result = await recommend_stocks(
            budget=500_000,
            market="kr",
            strategy="dividend",
            max_positions=2,
        )

        assert result["fallback_applied"] is True
        symbols = {item["symbol"] for item in result["recommendations"]}
        assert "555555" not in symbols
        assert "666666" in symbols
