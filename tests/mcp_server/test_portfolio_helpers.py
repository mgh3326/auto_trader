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
        # ROB-541: no source/broker -> routability fields are not fabricated.
        assert "order_routable" not in output
        assert "account_mode" not in output

    def _base_position(self, **overrides: object) -> dict:
        position = {
            "symbol": "005930",
            "name": "삼성전자",
            "market": "kr",
            "quantity": 10,
            "avg_buy_price": 70000,
            "current_price": 75000,
            "evaluation_amount": 750000,
            "profit_loss": 50000,
            "profit_rate": 7.14,
        }
        position.update(overrides)
        return position

    def test_kis_source_routable_true(self) -> None:
        # ROB-541: per-position order_routable + account_mode mirror get_holdings.
        output = position_to_output(self._base_position(source="kis_api", broker="kis"))
        assert output["order_routable"] is True
        assert output["account_mode"] == "kis_live"

    def test_toss_api_source_routable_false(self) -> None:
        output = position_to_output(
            self._base_position(source="toss_api", broker="toss")
        )
        assert output["order_routable"] is False
        assert output["account_mode"] == "toss_api"

    def test_manual_source_routable_false(self) -> None:
        output = position_to_output(
            self._base_position(account="samsung", broker="samsung", source="manual")
        )
        assert output["order_routable"] is False

    def test_upbit_source_provenance_label(self) -> None:
        output = position_to_output(
            self._base_position(
                symbol="KRW-BTC", market="crypto", broker="upbit", source="upbit_api"
            )
        )
        assert output["order_routable"] is True
        assert output["account_mode"] == "upbit_live"

    def test_routing_mode_respected_for_kis_mock(self) -> None:
        # When the position carries routing_mode (stamped by get_holdings),
        # the per-position account_mode matches the GROUP label exactly.
        output = position_to_output(
            self._base_position(source="kis_api", broker="kis", routing_mode="kis_mock")
        )
        assert output["account_mode"] == "kis_mock"
