from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.account_routing import (
    DEFAULT_ACCOUNT_COSTS,
    AccountRoutingInput,
    build_cost_profiles,
    suggest_account_from_snapshot,
)


def _cash(
    *, kis_domestic=2_000_000, kis_overseas=2_000, toss_krw=1_000_000, toss_usd=500
):
    return {
        "accounts": [
            {
                "account": "kis_domestic",
                "broker": "kis",
                "currency": "KRW",
                "orderable": float(kis_domestic),
            },
            {
                "account": "kis_overseas",
                "broker": "kis",
                "currency": "USD",
                "orderable": float(kis_overseas),
            },
            {
                "account": "toss",
                "broker": "toss",
                "currency": "KRW",
                "orderable": float(toss_krw),
            },
            {
                "account": "toss",
                "broker": "toss",
                "currency": "USD",
                "orderable": float(toss_usd),
            },
        ],
        "summary": {"exchange_rate_usd_krw": 1500.0},
        "errors": [],
    }


def _holdings(accounts: list[str]):
    return {
        "accounts": [
            {
                "account": account,
                "positions": [
                    {
                        "symbol": "005930" if account != "kis_overseas" else "AAPL",
                        "quantity": 1,
                        "evaluation_amount": 100_000,
                    }
                ],
            }
            for account in accounts
        ],
        "errors": [],
    }


def _costs_with_kis_kr_commission(commission_bps: float):
    costs = deepcopy(DEFAULT_ACCOUNT_COSTS)
    costs["accounts"]["kis_domestic"]["markets"]["kr"]["commission_bps"] = (
        commission_bps
    )
    return costs


def test_default_cost_profiles_are_review_required_until_operator_override():
    profiles = build_cost_profiles(None)

    assert profiles.source == "default_seed"
    assert profiles.review_required is True
    assert profiles.threshold_bps("kr") == pytest.approx(25)
    assert profiles.threshold_bps("us") == pytest.approx(40)
    assert profiles.market_profile(
        "kis_domestic", "kr"
    ).commission_bps == pytest.approx(14.7)
    assert profiles.market_profile("toss", "us").commission_bps == pytest.approx(10)


def test_invalid_cost_profile_values_fall_back_to_review_required_defaults():
    profiles = build_cost_profiles(
        {
            "version": 1,
            "routing": {"position_consolidation_threshold_bps": {"kr": "bad"}},
            "accounts": {
                "kis_domestic": {
                    "markets": {
                        "kr": {
                            "commission_bps": "bad",
                            "fx_spread_bps": "bad",
                        }
                    }
                },
                "toss": {"limits": {"max_order_notional_krw": "bad"}},
            },
        }
    )

    assert profiles.review_required is True
    assert profiles.threshold_bps("kr") == pytest.approx(25)
    assert profiles.market_profile(
        "kis_domestic", "kr"
    ).commission_bps == pytest.approx(14.7)
    assert profiles.market_profile("kis_domestic", "kr").fx_spread_bps == pytest.approx(
        0
    )
    assert profiles.max_order_notional_krw("toss") == pytest.approx(1_000_000)


def test_no_existing_holding_recommends_cheapest_eligible_account():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["success"] is True
    assert result["recommended_account"] == "toss"
    assert result["cost_comparison"]["kis_domestic"]["total_cost_krw"] == pytest.approx(
        1102.5
    )
    assert result["cost_comparison"]["toss"]["total_cost_krw"] == pytest.approx(0)
    assert result["position_consolidation"]["decision"] == "no_existing_position"


def test_existing_kr_holding_keeps_existing_when_savings_below_threshold():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings(["kis_domestic"]),
        )
    )

    assert result["recommended_account"] == "kis_domestic"
    assert result["position_consolidation"]["threshold_bps"] == pytest.approx(25)
    assert result["position_consolidation"]["threshold_amount_krw"] == pytest.approx(
        1875
    )
    assert result["position_consolidation"]["savings_vs_existing_krw"] == pytest.approx(
        1102.5
    )
    assert result["position_consolidation"]["foregone_savings_krw"] == pytest.approx(
        1102.5
    )
    assert result["position_consolidation"]["distribution_warning"] is False
    assert "existing_position_below_threshold" in result["reason_codes"]


def test_kis_holding_alias_maps_to_domestic_routing_account_for_kr():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot={
                "accounts": [
                    {
                        "account": "kis",
                        "broker": "kis",
                        "positions": [{"symbol": "005930", "quantity": 1}],
                    }
                ],
                "errors": [],
            },
        )
    )

    assert result["recommended_account"] == "kis_domestic"
    assert result["position_consolidation"]["existing_accounts"] == ["kis_domestic"]
    assert result["position_consolidation"]["decision"] == "keep_existing"


def test_kis_holding_alias_maps_to_overseas_routing_account_for_us():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="AAPL",
            market="us",
            side="buy",
            quantity=2,
            price=100,
            usd_krw=1500,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot={
                "accounts": [
                    {
                        "account": "kis",
                        "broker": "kis",
                        "positions": [{"symbol": "AAPL", "quantity": 1}],
                    }
                ],
                "errors": [],
            },
        )
    )

    assert result["recommended_account"] == "kis_overseas"
    assert result["position_consolidation"]["existing_accounts"] == ["kis_overseas"]
    assert result["position_consolidation"]["decision"] == "keep_existing"


def test_existing_kr_holding_breaks_consolidation_when_savings_exceed_threshold():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=_costs_with_kis_kr_commission(40.0),
            capital_snapshot=_cash(toss_krw=1_000_000),
            holdings_snapshot=_holdings(["kis_domestic"]),
        )
    )

    assert result["recommended_account"] == "toss"
    assert result["position_consolidation"]["decision"] == "break_for_cost"
    assert result["position_consolidation"]["distribution_warning"] is True
    assert "distribution_warning" in result["reason_codes"]


def test_existing_us_holding_uses_stronger_40_bps_threshold():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="AAPL",
            market="us",
            side="buy",
            quantity=2,
            price=100,
            usd_krw=1500,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(),
            holdings_snapshot=_holdings(["kis_overseas"]),
        )
    )

    assert result["recommended_account"] == "kis_overseas"
    assert result["position_consolidation"]["threshold_bps"] == pytest.approx(40)
    assert result["position_consolidation"]["distribution_warning"] is False
    assert result["cost_comparison"]["kis_overseas"][
        "fx_notional_krw"
    ] == pytest.approx(0)
    assert any("tax lots" in note.lower() for note in result["notes"])


def test_toss_notional_cap_makes_toss_ineligible_and_falls_back_to_kis():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=20,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(kis_domestic=2_000_000, toss_krw=2_000_000),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["recommended_account"] == "kis_domestic"
    assert result["cost_comparison"]["toss"]["eligible"] is False
    assert (
        result["cost_comparison"]["toss"]["ineligible_reason"]
        == "notional_limit_exceeded"
    )


def test_no_eligible_accounts_returns_failure_with_both_rows():
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol="005930",
            market="kr",
            side="buy",
            quantity=10,
            price=75_000,
            usd_krw=None,
            account_costs=DEFAULT_ACCOUNT_COSTS,
            capital_snapshot=_cash(kis_domestic=0, toss_krw=0),
            holdings_snapshot=_holdings([]),
        )
    )

    assert result["success"] is False
    assert result["recommended_account"] is None
    assert set(result["cost_comparison"]) == {"kis_domestic", "toss"}
    assert (
        result["cost_comparison"]["kis_domestic"]["ineligible_reason"]
        == "insufficient_orderable_cash"
    )
    assert (
        result["cost_comparison"]["toss"]["ineligible_reason"]
        == "insufficient_orderable_cash"
    )
