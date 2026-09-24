"""Unit tests for app.mcp_server.tooling.portfolio_helpers."""

from __future__ import annotations

import math

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
        assert result["unpriced_position_count"] == 0
        assert result["unpriced_buy_amount"] == 0

    def test_profit_loss_matches_evaluation_minus_buy_when_row_profit_missing(
        self,
    ) -> None:
        # Issue #236 reproduction fixture (2026-09-14 observation): a
        # broker-sourced KR position reports profit_loss, while a manual
        # position carries buy/evaluation but profit_loss=None. Before the fix
        # the summary summed only populated per-position profit_loss values,
        # reporting -6,696,447 / -7.62% while buy/evaluation implied
        # +12,056,958 / +13.72%.
        positions = [
            {
                "symbol": "005930",
                "name": "KIS KR holding",
                "avg_buy_price": 30_000_000 / 400,
                "quantity": 400,
                "evaluation_amount": 23_303_553,
                "profit_loss": -6_696_447,
                "profit_rate": -22.32,
            },
            {
                "symbol": "000660",
                "name": "pension manual KR holding",
                "avg_buy_price": 57_880_000 / 694,
                "quantity": 694,
                "evaluation_amount": 76_633_405,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 87_880_000
        assert result["total_evaluation"] == 99_936_958
        assert result["priced_buy_amount"] == 87_880_000
        assert result["total_profit_loss"] == pytest.approx(12_056_958.0)
        assert result["total_profit_rate"] == pytest.approx(13.72)
        assert result["unpriced_position_count"] == 0
        assert result["unknown_cost_position_count"] == 0

    def test_profit_loss_excludes_unpriced_cost_without_fabricating_loss(
        self,
    ) -> None:
        # A position without evaluation_amount is unpriced: its cost must not
        # be counted as a loss, and the excluded cost is disclosed.
        positions = [
            {
                "symbol": "005930",
                "name": "priced",
                "avg_buy_price": 1000,
                "quantity": 10,
                "evaluation_amount": 11_000,
                "profit_loss": 900,
                "profit_rate": 9.0,
            },
            {
                "symbol": "999999",
                "name": "unpriced",
                "avg_buy_price": 2000,
                "quantity": 5,
                "evaluation_amount": None,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 20_000
        assert result["total_evaluation"] == 11_000
        # Invariant: profit == evaluation - priced_buy = 11000 - 10000.
        assert result["unpriced_position_count"] == 1
        assert result["unpriced_buy_amount"] == 10_000
        assert result["priced_buy_amount"] == 10_000
        assert result["total_profit_loss"] == pytest.approx(1000.0)
        assert result["total_profit_rate"] == pytest.approx(10.0)

    def test_profit_loss_sign_matches_evaluation_minus_buy(self) -> None:
        # Even when the broker-reported row profit disagrees in sign with
        # evaluation minus buy, the summary must follow the same-object totals.
        positions = [
            {
                "symbol": "005930",
                "name": "net-fee broker P&L",
                "avg_buy_price": 1000,
                "quantity": 10,
                "evaluation_amount": 10_500,
                "profit_loss": -100,
                "profit_rate": -1.0,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_profit_loss"] == pytest.approx(500.0)
        assert result["total_profit_loss"] > 0
        assert result["total_profit_rate"] == pytest.approx(5.0)

    def test_all_unpriced_positions_yield_null_profit(self) -> None:
        positions = [
            {
                "symbol": "999999",
                "name": "unpriced",
                "avg_buy_price": 2000,
                "quantity": 5,
                "evaluation_amount": None,
                "profit_loss": None,
                "profit_rate": None,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 10_000
        assert result["total_evaluation"] == 0
        assert result["total_profit_loss"] is None
        assert result["total_profit_rate"] is None
        assert result["unpriced_position_count"] == 1
        assert result["unpriced_buy_amount"] == 10_000

    def test_unknown_cost_position_disclosed_not_counted_as_profit(self) -> None:
        # avg_buy_price <= 0 (e.g. Upbit external deposit / airdrop projected
        # with averageCost=None -> 0.0): the position's value is disclosed but
        # its evaluation is not silently counted as profit.
        positions = [
            {
                "symbol": "005930",
                "name": "priced",
                "avg_buy_price": 1000,
                "quantity": 10,
                "evaluation_amount": 11_000,
                "profit_loss": 1000,
                "profit_rate": 10.0,
            },
            {
                "symbol": "KRW-XRP",
                "name": "external deposit",
                "avg_buy_price": 0,
                "quantity": 300,
                "evaluation_amount": 900_000,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 10_000
        assert result["total_evaluation"] == 911_000
        # Invariant: profit = eval - unknown_cost_eval - priced_buy.
        assert result["unknown_cost_position_count"] == 1
        assert result["unknown_cost_evaluation_amount"] == 900_000
        assert result["unpriced_position_count"] == 0
        assert result["priced_buy_amount"] == 10_000
        assert result["total_profit_loss"] == pytest.approx(1000.0)
        assert result["total_profit_rate"] == pytest.approx(10.0)

    def test_unknown_cost_only_positions_yield_null_profit(self) -> None:
        positions = [
            {
                "symbol": "KRW-XRP",
                "name": "external deposit",
                "avg_buy_price": 0,
                "quantity": 300,
                "evaluation_amount": 500_000,
                "profit_loss": None,
                "profit_rate": None,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_profit_loss"] is None
        assert result["total_profit_rate"] is None
        assert result["unknown_cost_position_count"] == 1
        assert result["unknown_cost_evaluation_amount"] == 500_000

    def test_eval_zero_position_is_priced_as_total_loss(self) -> None:
        # evaluation_amount=0 (e.g. delisted) is a real valuation, not
        # "unpriced" — the position counts as a full loss.
        positions = [
            {
                "symbol": "000000",
                "name": "delisted",
                "avg_buy_price": 1000,
                "quantity": 10,
                "evaluation_amount": 0,
                "profit_loss": -10_000,
                "profit_rate": -100.0,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_profit_loss"] == pytest.approx(-10_000.0)
        assert result["total_profit_rate"] == pytest.approx(-100.0)
        assert result["unpriced_position_count"] == 0

    def test_empty_positions_profit_is_none(self) -> None:
        result = build_holdings_summary([], include_current_price=True)
        assert result["total_evaluation"] == 0
        assert result["total_profit_loss"] is None
        assert result["total_profit_rate"] is None
        assert result["unpriced_position_count"] == 0
        assert result["unpriced_buy_amount"] == 0
        assert result["unknown_cost_position_count"] == 0

    def test_no_current_price_branch_reports_all_unpriced(self) -> None:
        positions = [
            {"symbol": "005930", "avg_buy_price": 100, "quantity": 2},
        ]
        result = build_holdings_summary(positions, include_current_price=False)
        assert result["total_evaluation"] is None
        assert result["total_profit_loss"] is None
        assert result["unpriced_position_count"] == 1
        assert result["unpriced_buy_amount"] == 200
        assert result["unknown_cost_position_count"] == 0
        assert result["unknown_cost_evaluation_amount"] is None

    def test_non_finite_evaluation_is_unpriced_not_propagated(self) -> None:
        # A NaN/Infinity evaluation has no usable valuation; it is disclosed as
        # unpriced and must not poison the P&L totals.
        positions = [
            {
                "symbol": "005930",
                "avg_buy_price": 1000,
                "quantity": 10,
                "evaluation_amount": 9000,
            },
            {
                "symbol": "000000",
                "avg_buy_price": 500,
                "quantity": 1,
                "evaluation_amount": "NaN",
            },
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["unpriced_position_count"] == 1
        assert result["unpriced_buy_amount"] == 500
        assert result["total_evaluation"] == 9000
        assert result["total_profit_loss"] == pytest.approx(-1000.0)
        assert result["total_profit_rate"] == pytest.approx(-10.0)
        assert math.isfinite(result["total_profit_loss"])

    def test_non_finite_cost_basis_is_unknown_cost(self) -> None:
        positions = [
            {
                "symbol": "KRW-XRP",
                "avg_buy_price": "Infinity",
                "quantity": 300,
                "evaluation_amount": 500_000,
            }
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["unknown_cost_position_count"] == 1
        assert result["unknown_cost_evaluation_amount"] == 500_000
        assert result["total_evaluation"] == 500_000
        assert result["total_profit_loss"] is None
        assert result["total_profit_rate"] is None


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
