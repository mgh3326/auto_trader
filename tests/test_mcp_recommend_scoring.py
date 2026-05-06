from __future__ import annotations

import pytest

from app.mcp_server.scoring import (
    calc_composite_score,
    calc_dividend_score,
    calc_momentum_score,
    calc_rsi_score,
    calc_valuation_score,
    generate_reason,
)
from app.mcp_server.strategies import (
    VALID_STRATEGIES,
    get_strategy_config,
    validate_strategy,
)
from app.mcp_server.tooling import analysis_recommend
from tests._mcp_tooling_support import build_tools


class TestScoringFunctions:
    def test_calc_rsi_score_handles_none(self):
        assert calc_rsi_score(None) == pytest.approx(50.0)

    def test_calc_valuation_score_handles_none(self):
        assert calc_valuation_score(None, None) == pytest.approx(50.0)

    def test_calc_momentum_score_handles_none(self):
        assert calc_momentum_score(None) == pytest.approx(50.0)

    def test_calc_dividend_score_accepts_percent_input(self):
        score_decimal = calc_dividend_score(0.05)
        score_percent = calc_dividend_score(5.0)
        assert score_decimal == score_percent

    def test_calc_composite_score_range(self):
        score = calc_composite_score(
            {
                "rsi": 42,
                "per": 12,
                "pbr": 1.2,
                "change_rate": 2.1,
                "volume": 10_000_000,
                "dividend_yield": 0.03,
            }
        )
        assert 0 <= score <= 100

    def test_calc_composite_score_crypto_liquidity_weighting(self):
        high_liquidity = calc_composite_score(
            {
                "market": "crypto",
                "rsi": 50,
                "change_rate": 1.0,
                "volume": 1_000_000,
                "trade_amount_24h": 150_000_000_000,
            }
        )
        low_liquidity = calc_composite_score(
            {
                "market": "crypto",
                "rsi": 50,
                "change_rate": 1.0,
                "volume": 1_000_000,
                "trade_amount_24h": 500_000_000,
            }
        )
        assert high_liquidity > low_liquidity

    def test_calc_composite_score_uses_rsi_field_only(self):
        score_with_legacy_only = calc_composite_score({"rsi_14": 10})
        score_without_rsi = calc_composite_score({})
        assert score_with_legacy_only == score_without_rsi

    def test_generate_reason_ignores_legacy_rsi_14_field(self):
        reason = generate_reason({"rsi_14": 22.0}, strategy="balanced")
        assert "RSI" not in reason


class TestCryptoReasonBuilder:
    def test_build_crypto_rsi_reason_rsi_only(self):
        reason = analysis_recommend._build_crypto_rsi_reason(
            {
                "rsi": 33.2,
                "rsi_bucket": 30,
                "candle_type": "bullish",
                "volume_ratio": 1.4,
            }
        )

        assert "RSI 33.2" in reason
        assert "캔들 bullish" in reason
        assert "거래량 1.4배" in reason


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
        return analysis_recommend._allocate_budget

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

    def test_allocate_budget_equal_mode_dedupes_symbols(self):
        candidates = [
            {"symbol": "AAA", "price": 1000, "score": 80, "rsi": 35},
            {"symbol": "AAA", "price": 1000, "score": 70, "rsi": 40},
            {"symbol": "BBB", "price": 1000, "score": 60, "rsi": 45},
        ]

        allocated, remaining = analysis_recommend.allocate_budget(
            candidates,
            10_000,
            5,
            mode="equal",
        )

        symbols = [item["symbol"] for item in allocated]
        assert symbols.count("AAA") == 1
        assert symbols.count("BBB") == 1
        assert remaining == 0


def test_empty_recommend_response_keeps_contract_shape() -> None:
    result = analysis_recommend._empty_recommend_response(
        budget=1_000_000,
        strategy="balanced",
        strategy_description="Balanced allocation",
        warnings=["example"],
        diagnostics={"phase": "test"},
        fallback_applied=False,
    )

    assert result["recommendations"] == []
    assert result["warnings"] == ["example"]
    assert result["fallback_applied"] is False
    assert result["diagnostics"] == {"phase": "test"}


def test_prepare_recommend_request_returns_context_object_for_crypto() -> None:
    context = analysis_recommend._prepare_recommend_request(
        budget=1_000_000,
        market="crypto",
        strategy="balanced",
        exclude_symbols=None,
        sectors=["ignored-sector"],
        max_positions=3,
        exclude_held=True,
    )

    assert type(context).__name__ == "RecommendRequestContext"
    assert context.normalized_market == "crypto"
    assert context.screen_category is None
    assert context.sort_by == "rsi"
    assert context.sort_order == "asc"
    assert context.max_pbr is None
    assert any("crypto market" in warning for warning in context.warnings)


class TestCandidateNormalization:
    @pytest.fixture
    def normalize_candidate(self):
        build_tools()
        return analysis_recommend._normalize_candidate

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

    def test_rsi_bucket_zero_is_preserved_for_crypto(self, normalize_candidate):
        item = {
            "symbol": "KRW-BTC",
            "trade_price": 1000,
            "rsi_bucket": 0,
        }
        normalized = normalize_candidate(item, "crypto")
        assert normalized["rsi_bucket"] == 0

    def test_crypto_market_cap_none_remains_none(self, normalize_candidate):
        item = {
            "symbol": "KRW-BTC",
            "trade_price": 1000,
            "market_cap": None,
        }
        normalized = normalize_candidate(item, "crypto")
        assert normalized["market_cap"] is None

    def test_volume_24h_fallback_order(self, normalize_candidate):
        item_with_acc_trade_volume = {
            "symbol": "KRW-BTC",
            "trade_price": 1000,
            "acc_trade_volume_24h": 12345,
        }
        normalized_from_acc = normalize_candidate(item_with_acc_trade_volume, "crypto")
        assert normalized_from_acc["volume_24h"] == pytest.approx(12345.0)

        item_with_volume_only = {
            "symbol": "KRW-ETH",
            "trade_price": 1000,
            "volume": 6789,
        }
        normalized_from_volume = normalize_candidate(item_with_volume_only, "crypto")
        assert normalized_from_volume["volume_24h"] == pytest.approx(6789.0)
