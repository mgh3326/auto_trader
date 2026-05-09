"""Unit tests for app.mcp_server.tooling.portfolio_helpers."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.portfolio_helpers import (
    build_holdings_summary,
    min_order_krw,
    position_to_output,
    recalculate_profit_fields,
)


class TestBuildHoldingsSummary:
    def test_empty_no_current_price(self) -> None:
        result = build_holdings_summary([], include_current_price=False)
        assert result["total_buy_amount"] == 0
        assert result["position_count"] == 0
        assert result["weights"] is None
        assert result["total_evaluation"] is None

    def test_empty_with_current_price(self) -> None:
        result = build_holdings_summary([], include_current_price=True)
        assert result["total_evaluation"] == 0
        assert result["weights"] == []
        assert result["position_count"] == 0

    def test_single_position_with_current_price(self) -> None:
        positions = [
            {
                "symbol": "KRW-BTC",
                "name": "Bitcoin",
                "avg_buy_price": 1000,
                "quantity": 2,
                "evaluation_amount": 2100,
                "profit_loss": 100,
                "profit_rate": 5.0,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 2000
        assert result["total_evaluation"] == 2100
        assert result["total_profit_loss"] == 100
        assert result["total_profit_rate"] == pytest.approx(5.0, rel=1e-3)
        assert result["position_count"] == 1


class TestRecalculateProfitFields:
    def test_no_current_price_clears_fields(self) -> None:
        position: dict = {
            "current_price": None,
            "quantity": 10,
            "avg_buy_price": 1000,
        }
        recalculate_profit_fields(position)
        assert position["evaluation_amount"] is None
        assert position["profit_loss"] is None
        assert position["profit_rate"] is None

    def test_normal_profit_calculation(self) -> None:
        position: dict = {
            "current_price": 1100,
            "quantity": 2,
            "avg_buy_price": 1000,
        }
        recalculate_profit_fields(position)
        assert position["evaluation_amount"] == pytest.approx(2200.0)
        assert position["profit_loss"] == pytest.approx(200.0)
        assert position["profit_rate"] == pytest.approx(10.0)


class TestMinOrderKrw:
    def test_krw_btc_returns_5000(self) -> None:
        assert min_order_krw("KRW-BTC") == 5000.0

    def test_other_symbol_same_default(self) -> None:
        assert min_order_krw("KRW-ETH") == 5000.0


class TestPositionToOutput:
    def test_required_fields_present(self) -> None:
        position = {
            "symbol": "KRW-BTC",
            "name": "Bitcoin",
            "market": "crypto",
            "quantity": 1,
            "avg_buy_price": 50000000,
            "current_price": 52000000,
            "evaluation_amount": 52000000,
            "profit_loss": 2000000,
            "profit_rate": 4.0,
        }
        output = position_to_output(position)
        assert output["symbol"] == "KRW-BTC"
        assert output["dust"] is False
        assert "evaluation_amount" in output
        assert "profit_rate" in output

    def test_dust_defaults_false(self) -> None:
        position = {
            "symbol": "KRW-ETH",
            "name": "Ethereum",
            "market": "crypto",
            "quantity": 0,
            "avg_buy_price": 0,
            "current_price": 0,
            "evaluation_amount": 0,
            "profit_loss": 0,
            "profit_rate": 0,
        }
        output = position_to_output(position)
        assert output["dust"] is False
