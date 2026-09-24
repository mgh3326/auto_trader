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


_NON_FINITE_VALUES = [
    pytest.param("NaN", id="nan_str"),
    pytest.param("Infinity", id="pos_inf_str"),
    pytest.param("-Infinity", id="neg_inf_str"),
    pytest.param(float("nan"), id="nan_float"),
    pytest.param(math.inf, id="pos_inf_float"),
    pytest.param(-math.inf, id="neg_inf_float"),
]


def _finite_row(symbol: str, evaluation: float | None) -> dict:
    return {
        "symbol": symbol,
        "avg_buy_price": 1000,
        "quantity": 10,
        "evaluation_amount": evaluation,
    }


def _assert_finite_number(value: object) -> None:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert math.isfinite(value)


class TestHoldingsSummaryNonFiniteCost:
    """#634: a non-finite ``avg_buy_price``/``quantity`` must not reach the
    buy-amount totals. The row is excluded from ``total_buy_amount`` and
    ``unpriced_buy_amount`` and counted in ``non_finite_cost_position_count``,
    mirroring #236 (non-finite evaluation -> unpriced, non-finite cost ->
    unknown_cost; disclosed, never summed)."""

    @pytest.mark.parametrize("field", ["avg_buy_price", "quantity"])
    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES)
    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_non_finite_unpriced_row_excluded_from_buy_totals(
        self, field: str, bad: object, include_current_price: bool
    ) -> None:
        # Unpriced row (no evaluation): before #634 its NaN/Inf cost reached
        # both total_buy_amount and unpriced_buy_amount.
        bad_row = _finite_row("BAD", None)
        bad_row[field] = bad
        positions = [_finite_row("OK", 12_000), bad_row]
        result = build_holdings_summary(positions, include_current_price)

        _assert_finite_number(result["total_buy_amount"])
        _assert_finite_number(result["unpriced_buy_amount"])
        assert result["total_buy_amount"] == 10_000
        assert result["non_finite_cost_position_count"] == 1
        assert result["position_count"] == 2
        if include_current_price:
            assert result["unpriced_position_count"] == 1
            assert result["unpriced_buy_amount"] == 0
            assert result["priced_buy_amount"] == 10_000
            assert result["total_profit_loss"] == pytest.approx(2000.0)
        else:
            assert result["unpriced_position_count"] == 2
            assert result["unpriced_buy_amount"] == 10_000

    @pytest.mark.parametrize("field", ["avg_buy_price", "quantity"])
    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES)
    def test_non_finite_priced_row_excluded_from_total_buy(
        self, field: str, bad: object
    ) -> None:
        # Priced row with non-finite cost stays unknown_cost (#236) and is now
        # also kept out of total_buy_amount.
        bad_row = _finite_row("BAD", 7_000)
        bad_row[field] = bad
        positions = [_finite_row("OK", 12_000), bad_row]
        result = build_holdings_summary(positions, include_current_price=True)

        _assert_finite_number(result["total_buy_amount"])
        assert result["total_buy_amount"] == 10_000
        assert result["unpriced_buy_amount"] == 0
        assert result["non_finite_cost_position_count"] == 1
        assert result["unknown_cost_position_count"] == 1
        assert result["unknown_cost_evaluation_amount"] == 7_000
        assert result["priced_buy_amount"] == 10_000
        assert result["total_evaluation"] == 19_000
        assert result["total_profit_loss"] == pytest.approx(2000.0)

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES)
    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_both_fields_non_finite(
        self, bad: object, include_current_price: bool
    ) -> None:
        bad_row = {
            "symbol": "BAD",
            "avg_buy_price": bad,
            "quantity": bad,
            "evaluation_amount": None,
        }
        positions = [_finite_row("OK", None), bad_row]
        result = build_holdings_summary(positions, include_current_price)
        _assert_finite_number(result["total_buy_amount"])
        _assert_finite_number(result["unpriced_buy_amount"])
        assert result["total_buy_amount"] == 10_000
        assert result["unpriced_buy_amount"] == 10_000
        assert result["non_finite_cost_position_count"] == 1

    @pytest.mark.parametrize("bad", _NON_FINITE_VALUES)
    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_zero_quantity_with_non_finite_price(
        self, bad: object, include_current_price: bool
    ) -> None:
        # inf * 0 is NaN; a zero quantity does not rescue an unknown price.
        positions = [
            _finite_row("OK", None),
            {"symbol": "BAD", "avg_buy_price": bad, "quantity": 0},
        ]
        result = build_holdings_summary(positions, include_current_price)
        _assert_finite_number(result["total_buy_amount"])
        _assert_finite_number(result["unpriced_buy_amount"])
        assert result["total_buy_amount"] == 10_000
        assert result["unpriced_buy_amount"] == 10_000
        assert result["non_finite_cost_position_count"] == 1

    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_zero_quantity_finite_price_is_not_flagged(
        self, include_current_price: bool
    ) -> None:
        positions = [
            _finite_row("OK", None),
            {"symbol": "ZERO", "avg_buy_price": 1000, "quantity": 0},
        ]
        result = build_holdings_summary(positions, include_current_price)
        assert result["total_buy_amount"] == 10_000
        assert result["unpriced_buy_amount"] == 10_000
        assert result["unpriced_position_count"] == 2
        assert result["non_finite_cost_position_count"] == 0

    def test_finite_overflow_product_is_excluded(self) -> None:
        # Both inputs finite but the product overflows to inf.
        positions = [
            _finite_row("OK", None),
            {"symbol": "HUGE", "avg_buy_price": 1e200, "quantity": 1e200},
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        _assert_finite_number(result["total_buy_amount"])
        assert result["total_buy_amount"] == 10_000
        assert result["unpriced_buy_amount"] == 10_000
        assert result["non_finite_cost_position_count"] == 1

    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_mixed_finite_and_non_finite_rows(
        self, include_current_price: bool
    ) -> None:
        positions = [
            _finite_row("P1", 11_000),  # priced, buy 10_000
            {
                "symbol": "P2",
                "avg_buy_price": 250,
                "quantity": 4,
                "evaluation_amount": 900,
            },  # priced, buy 1_000
            {
                "symbol": "U1",
                "avg_buy_price": 300,
                "quantity": 5,
                "evaluation_amount": None,
            },  # unpriced, buy 1_500
            {
                "symbol": "X1",
                "avg_buy_price": "NaN",
                "quantity": 3,
                "evaluation_amount": None,
            },
            {
                "symbol": "X2",
                "avg_buy_price": 10,
                "quantity": math.inf,
                "evaluation_amount": 50,
            },
            {
                "symbol": "X3",
                "avg_buy_price": -math.inf,
                "quantity": 2,
                "evaluation_amount": "Infinity",
            },
            {
                "symbol": "X4",
                "avg_buy_price": "Infinity",
                "quantity": 0,
                "evaluation_amount": None,
            },
        ]
        result = build_holdings_summary(positions, include_current_price)
        _assert_finite_number(result["total_buy_amount"])
        _assert_finite_number(result["unpriced_buy_amount"])
        assert result["total_buy_amount"] == 12_500
        assert result["non_finite_cost_position_count"] == 4
        assert result["position_count"] == 7
        if include_current_price:
            # U1, X1, X3 (non-finite evaluation), X4 are unpriced; only U1
            # has a finite cost to disclose.
            assert result["unpriced_position_count"] == 4
            assert result["unpriced_buy_amount"] == 1_500
            assert result["unknown_cost_position_count"] == 1  # X2
            assert result["unknown_cost_evaluation_amount"] == 50
            assert result["priced_buy_amount"] == 11_000
            assert result["total_evaluation"] == 11_950
            assert result["total_profit_loss"] == pytest.approx(900.0)
        else:
            assert result["unpriced_position_count"] == 7
            assert result["unpriced_buy_amount"] == 12_500

    @pytest.mark.parametrize("include_current_price", [True, False])
    def test_finite_rows_byte_identical_to_pre_fix_formula(
        self, include_current_price: bool
    ) -> None:
        positions = [
            {
                "symbol": "A",
                "avg_buy_price": 0.1,
                "quantity": 3,
                "evaluation_amount": 0.35,
            },
            {
                "symbol": "B",
                "avg_buy_price": "71234.567",
                "quantity": "13",
                "evaluation_amount": None,
            },
            {
                "symbol": "C",
                "avg_buy_price": 1e-9,
                "quantity": 7,
                "evaluation_amount": 1,
            },
            {
                "symbol": "D",
                "avg_buy_price": None,
                "quantity": 5,
                "evaluation_amount": None,
            },
            {
                "symbol": "E",
                "avg_buy_price": -3.3,
                "quantity": 2,
                "evaluation_amount": None,
            },
            {
                "symbol": "F",
                "avg_buy_price": 88_000,
                "quantity": 0.123456,
                "evaluation_amount": 10_000,
            },
        ]

        def _buy(p: dict) -> float:
            def _f(v: object) -> float:
                return 0.0 if v in (None, "") else float(v)  # type: ignore[arg-type]

            return _f(p.get("avg_buy_price")) * _f(p.get("quantity"))

        expected_total = round(sum(_buy(p) for p in positions), 2)
        result = build_holdings_summary(positions, include_current_price)
        assert repr(result["total_buy_amount"]) == repr(expected_total)
        assert result["non_finite_cost_position_count"] == 0
        if include_current_price:
            expected_unpriced = round(
                sum(_buy(p) for p in positions if p["evaluation_amount"] is None),
                2,
            )
        else:
            expected_unpriced = expected_total
        assert repr(result["unpriced_buy_amount"]) == repr(expected_unpriced)

    def test_all_rows_non_finite_totals_are_zero(self) -> None:
        positions = [
            {
                "symbol": "X",
                "avg_buy_price": "NaN",
                "quantity": 1,
                "evaluation_amount": None,
            },
        ]
        result = build_holdings_summary(positions, include_current_price=True)
        assert result["total_buy_amount"] == 0
        assert result["unpriced_buy_amount"] == 0
        assert result["unpriced_position_count"] == 1
        assert result["non_finite_cost_position_count"] == 1
        assert result["total_profit_loss"] is None


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
