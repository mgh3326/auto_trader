"""§S177 auto-approval boundaries for cash-funding cash proxies."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)

_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("1500"),
    daily_cap=Decimal("20000"),
    policy_version="cash-funding-test",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)

_OFF_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("1500"),
    daily_cap=Decimal("20000"),
    policy_version="cash-funding-test",
    mode="off",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("90"),
)


def _group(**overrides):
    values = {
        "symbol": "SGOV",
        "market": "equity_us",
        "account_mode": "kis_live",
        "broker_account_id": "test-account",
        "order_type": "limit",
        "action": "place",
        "exit_intent": "cash_funding",
        "thesis": "Sell cash equivalent to fund approved USD buy.",
        "source_asof": {
            "cash_funding": {
                "funding_target": {
                    "market": "equity_us",
                    "required": "100",
                    "plan_ref": "planned-buy-001",
                }
            }
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "side": "sell",
        "limit_price": Decimal("99"),
        "quantity": Decimal("2"),
        "notional": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _decision(**overrides):
    values = {
        "group": _group(),
        "rung": _rung(),
        "preview": {
            "success": True,
            "current_price": "100",
            # Deliberately a loss: the cash-funding path must not require a
            # take-profit proof or it could never fund the planned buy.
            "avg_buy_price": "110",
            "realized_pnl": "-22",
        },
        "limits": _LIMITS,
        "daily_notional": Decimal("0"),
        "cash_funding_shortfall": Decimal("100"),
        "cash_funding_cumulative_notional": Decimal("0"),
    }
    values.update(overrides)
    return evaluate_auto_approve_eligibility(**values)


def test_cash_funding_reaches_auto_approve_within_all_boundaries():
    decision = _decision()

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details["loss_guard"] == "cash_funding_exempt"
    assert decision.details["marketability"] == "cash_funding_marketable"
    assert decision.details["notional"] == "200"
    assert decision.details["cash_funding_cumulative_after"] == "200"


def test_cash_funding_boundary_failures_demote_to_human_card():
    missing_target = _decision(
        group=_group(source_asof={"cash_funding": {}}),
    )
    unmeasured_shortfall = _decision(cash_funding_shortfall=None)

    assert (missing_target.eligible, missing_target.reason) == (
        False,
        "cash_funding_boundary_failed",
    )
    assert missing_target.details["cash_funding_reason"] == "funding_target_missing"
    assert (unmeasured_shortfall.eligible, unmeasured_shortfall.reason) == (
        False,
        "cash_funding_boundary_failed",
    )
    assert unmeasured_shortfall.details["cash_funding_reason"] == "shortfall_unmeasured"


def test_cash_funding_cumulative_cap_demotes_to_human_card():
    # max(limit, current) * quantity = 200; 9,801 + 200 exceeds 10,000 USD.
    decision = _decision(cash_funding_cumulative_notional=Decimal("9801"))

    assert (decision.eligible, decision.reason) == (
        False,
        "cash_funding_cumulative_cap_exceeded",
    )
    assert decision.details["cash_funding_cumulative_before"] == "9801"
    assert decision.details["cash_funding_cumulative_cap"] == "10000"


def test_non_cash_proxy_loss_sale_cannot_reach_cash_funding_auto_path():
    ordinary = _decision(
        group=_group(symbol="AAPL", exit_intent=None),
    )
    spoofed = _decision(group=_group(symbol="AAPL"))

    assert (ordinary.eligible, ordinary.reason) == (False, "expected_pnl_not_positive")
    assert (spoofed.eligible, spoofed.reason) == (
        False,
        "cash_funding_boundary_failed",
    )
    assert spoofed.details["cash_funding_reason"] == "symbol_not_cash_proxy"


@pytest.mark.parametrize(
    "group_overrides",
    (
        {"symbol": "AAPL"},
        {"symbol": "SGOV", "market": "equity_kr"},
    ),
    ids=("symbol_not_allowlisted", "allowlist_tuple_mismatch"),
)
def test_cash_funding_requires_exact_allowlist_tuple_before_raised_cap(
    group_overrides: dict[str, str],
):
    decision = _decision(group=_group(**group_overrides))

    assert (decision.eligible, decision.reason) == (
        False,
        "cash_funding_boundary_failed",
    )
    assert decision.details["cash_funding_reason"] == "symbol_not_cash_proxy"
    assert "per_order_cap" not in decision.details


def test_cash_funding_requires_sell_before_raised_cap():
    decision = _decision(rung=_rung(side="buy"))

    assert (decision.eligible, decision.reason) == (
        False,
        "cash_funding_boundary_failed",
    )
    assert decision.details["cash_funding_reason"] == "side_not_sell"
    assert "per_order_cap" not in decision.details


def test_cash_funding_cumulative_cap_gates_raised_cap_and_off_mode_distance():
    high_need_group = _group(
        source_asof={
            "cash_funding": {
                "funding_target": {
                    "market": "equity_us",
                    "required": "1900",
                    "plan_ref": "planned-buy-1900",
                }
            }
        }
    )
    high_need_rung = _rung(quantity=Decimal("20"))
    cumulative_denied = _decision(
        group=high_need_group,
        rung=high_need_rung,
        limits=_OFF_LIMITS,
        cash_funding_shortfall=Decimal("1900"),
        cash_funding_cumulative_notional=Decimal("9000"),
    )
    allowed = _decision(
        group=high_need_group,
        rung=high_need_rung,
        limits=_OFF_LIMITS,
        cash_funding_shortfall=Decimal("1900"),
        cash_funding_cumulative_notional=Decimal("0"),
    )

    # 2,000 clears only the immutable USD 10,000 raised cap.  The second
    # boundary still demotes the same rung once its projected sum is 11,000.
    assert (cumulative_denied.eligible, cumulative_denied.reason) == (
        False,
        "cash_funding_cumulative_cap_exceeded",
    )
    assert cumulative_denied.details["cash_funding_cumulative_before"] == "9000"
    assert cumulative_denied.details["cash_funding_cumulative_after"] == "11000"
    assert cumulative_denied.details["cash_funding_cumulative_cap"] == "10000"
    assert "per_order_cap" not in cumulative_denied.details
    # This is deliberately `off`: the cash-funding sell authorization is
    # independent of the §163 parking-buy mode and skips the sell distance
    # floor only after all of its own boundaries pass.
    assert (allowed.eligible, allowed.reason) == (True, "eligible")
    assert allowed.details["mode"] == "off"
    assert allowed.details["per_order_cap"] == "10000"
    assert allowed.details["marketability"] == "cash_funding_marketable"


def test_inactive_cash_funding_keeps_ordinary_cap_in_off_mode():
    ordinary = _decision(
        group=_group(exit_intent=None),
        rung=_rung(quantity=Decimal("20")),
        limits=_OFF_LIMITS,
    )

    assert (ordinary.eligible, ordinary.reason) == (False, "per_order_cap_exceeded")
    assert ordinary.details["per_order_cap"] == "1500"
    assert ordinary.details["notional"] == "1980"
