"""L8 pure metrics: exact Decimal values and missing evidence stay explicit."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.retro_v1.metrics import (
    DailyBar,
    RiskRung,
    calculate_costs,
    calculate_mfe_mae,
    calculate_risk_amount,
)

pytestmark = pytest.mark.unit


def test_risk_amount_sums_risk_per_rung_and_uses_recorded_inputs() -> None:
    result = calculate_risk_amount(
        side="buy",
        stop_price=Decimal("80"),
        stop_basis="recorded",
        rungs=[
            RiskRung(
                planned_quantity=Decimal("1"),
                reference_price=Decimal("100"),
                reference_price_kind="limit",
                reference_price_basis="recorded",
            ),
            RiskRung(
                planned_quantity=Decimal("1"),
                reference_price=Decimal("90"),
                reference_price_kind="limit",
                reference_price_basis="recorded",
            ),
        ],
        budget_cap=Decimal("300"),
    )

    assert result.amount == Decimal("30")
    assert result.pct_of_notional == Decimal("30") / Decimal("190")
    assert result.pct_of_budget == Decimal("0.1")
    assert result.pct_of_budget_reason is None
    assert result.reference_price_kinds == ("limit", "limit")
    assert result.basis == "recorded"
    assert result.reason is None


def test_risk_amount_does_not_return_a_partial_sum_when_a_rung_lacks_price() -> None:
    result = calculate_risk_amount(
        side="buy",
        stop_price=Decimal("80"),
        stop_basis="recorded",
        rungs=[
            RiskRung(Decimal("1"), Decimal("100"), "limit", "recorded"),
            RiskRung(Decimal("2"), None, None, None),
        ],
    )

    assert result.amount is None
    assert result.pct_of_notional is None
    assert result.reason == "no_reference_price"


@pytest.mark.parametrize(
    ("side", "stop", "stop_basis", "rungs", "reason"),
    [
        ("buy", None, None, [], "no_stop_recorded"),
        (
            "buy",
            Decimal("100"),
            "recorded",
            [RiskRung(Decimal("1"), Decimal("100"), "limit", "recorded")],
            "stop_not_below_reference_price",
        ),
        (
            "buy",
            Decimal("80"),
            "recorded",
            [RiskRung(None, Decimal("100"), "limit", "recorded")],
            "no_planned_quantity",
        ),
        ("sell", Decimal("80"), "recorded", [], "not_applicable"),
    ],
)
def test_risk_amount_missing_or_inapplicable_inputs_return_null_with_reason(
    side: str,
    stop: Decimal | None,
    stop_basis: str | None,
    rungs: list[RiskRung],
    reason: str,
) -> None:
    result = calculate_risk_amount(
        side=side,  # type: ignore[arg-type]
        stop_price=stop,
        stop_basis=stop_basis,  # type: ignore[arg-type]
        rungs=rungs,
    )

    assert result.amount is None
    assert result.reason == reason


def test_daily_mfe_mae_excludes_entry_and_exit_sessions() -> None:
    result = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 9),
        bars=[
            DailyBar(date(2026, 1, 5), Decimal("130"), Decimal("70")),
            DailyBar(date(2026, 1, 6), Decimal("108"), Decimal("95")),
            DailyBar(date(2026, 1, 7), Decimal("107"), Decimal("96")),
            DailyBar(date(2026, 1, 9), Decimal("140"), Decimal("60")),
        ],
        risk_per_unit=Decimal("10"),
    )

    assert result.mfe.amount == Decimal("8")
    assert result.mae.amount == Decimal("5")
    assert result.mfe.status == result.mae.status == "complete"
    assert result.adjusted is None
    assert result.basis == "inference"


def test_daily_mfe_and_mae_clamp_below_zero_excursions_to_zero() -> None:
    down = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 7),
        bars=[DailyBar(date(2026, 1, 6), Decimal("99"), Decimal("95"))],
    )
    up = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 7),
        bars=[DailyBar(date(2026, 1, 6), Decimal("105"), Decimal("101"))],
    )

    assert down.mfe.amount == Decimal("0")
    assert up.mae.amount == Decimal("0")


def test_daily_mfe_mae_measures_only_contained_bars_and_returns_amount_pct_and_r() -> None:
    result = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 9),
        bars=[
            DailyBar(date(2026, 1, 6), Decimal("108"), Decimal("95")),
            DailyBar(date(2026, 1, 7), Decimal("96"), Decimal("88")),
        ],
        risk_per_unit=Decimal("10"),
    )

    assert result.mfe.amount == Decimal("8")
    assert result.mfe.pct == Decimal("0.08")
    assert result.mfe.r == Decimal("0.8")
    assert result.mae.amount == Decimal("12")
    assert result.mae.pct == Decimal("0.12")
    assert result.mae.r == Decimal("1.2")


def test_daily_mfe_mae_has_no_complete_session_when_entry_and_exit_are_adjacent() -> None:
    result = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 6),
        bars=[DailyBar(date(2026, 1, 5), Decimal("101"), Decimal("99"))],
    )

    assert result.mfe.amount is None
    assert result.mae.amount is None
    assert result.mfe.status == "unknown"
    assert result.mfe.reason == result.mae.reason == "no_complete_session"
    assert result.adjusted is None
    assert result.basis == "inference"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"entry_price": None, "entry_session": date(2026, 1, 5)}, "missing_entry_price"),
        ({"entry_price": Decimal("100"), "entry_session": None}, "missing_entry_session"),
    ],
)
def test_daily_mfe_mae_missing_inputs_return_null_with_reason(
    kwargs: dict[str, object], reason: str
) -> None:
    result = calculate_mfe_mae(bars=[], **kwargs)  # type: ignore[arg-type]

    assert result.mfe.amount is None
    assert result.mae.amount is None
    assert result.mfe.reason == result.mae.reason == reason


def test_daily_mfe_mae_explains_missing_risk_per_unit_for_r_multiple() -> None:
    result = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        exit_session=date(2026, 1, 7),
        bars=[DailyBar(date(2026, 1, 6), Decimal("105"), Decimal("95"))],
    )

    assert result.mfe.amount == Decimal("5")
    assert result.mfe.r is None
    assert result.mfe.r_reason == "no_risk_per_unit"


def test_daily_mfe_mae_waits_for_an_open_horizon() -> None:
    result = calculate_mfe_mae(
        entry_price=Decimal("100"),
        entry_session=date(2026, 1, 5),
        bars=[DailyBar(date(2026, 1, 6), Decimal("105"), Decimal("95"))],
        horizon_sessions=2,
    )

    assert result.mfe.amount is None
    assert result.mae.amount is None
    assert result.mfe.status == "pending"
    assert result.mfe.reason == result.mae.reason == "window_open"


def test_costs_keep_recorded_values_separate_from_policy_estimate() -> None:
    result = calculate_costs(
        fee=Decimal("1.25"),
        tax=Decimal("0.50"),
        currency="USD",
        fee_basis="broker_reported",
        notional=Decimal("1000"),
        round_trip_cost_bps=Decimal("90"),
        policy_version="v7",
        policy_content_hash="a" * 64,
    )

    assert result.recorded is not None
    assert result.recorded.fee == Decimal("1.25")
    assert result.recorded.tax == Decimal("0.50")
    assert result.recorded.currency == "USD"
    assert result.estimated is not None
    assert result.estimated.amount == Decimal("9")
    assert result.estimated.rate_source == (
        "trading_policy.order_proposals.auto_approve.round_trip_cost_bps"
    )
    assert result.estimated.policy_version == "v7"
    assert result.estimated.policy_content_hash == "a" * 64
    assert result.estimated.basis == "inference"


def test_costs_treat_unprovided_cost_as_null_and_explain_missing_estimate_input() -> None:
    result = calculate_costs(
        fee=None,
        tax=None,
        currency="KRW",
        fee_basis="not_provided",
        notional=None,
        round_trip_cost_bps=Decimal("47.4"),
        policy_version=None,
        policy_content_hash=None,
    )

    assert result.recorded is None
    assert result.recorded_reason == "not_provided"
    assert result.estimated is None
    assert result.estimated_reason == "missing_notional"


def test_costs_report_missing_rate_without_inventing_an_estimate() -> None:
    result = calculate_costs(
        fee=None,
        tax=None,
        currency="KRW",
        fee_basis="not_provided",
        notional=Decimal("1000"),
        round_trip_cost_bps=None,
        policy_version=None,
        policy_content_hash=None,
    )

    assert result.estimated is None
    assert result.estimated_reason == "rate_unavailable"


def test_costs_preserve_reported_zero_fee_as_a_recorded_value() -> None:
    result = calculate_costs(
        fee=Decimal("0"),
        tax=None,
        currency="KRW",
        fee_basis="broker_reported",
        notional=Decimal("1000"),
        round_trip_cost_bps=Decimal("47.4"),
        policy_version="v7",
        policy_content_hash="b" * 64,
    )

    assert result.recorded == type(result.recorded)(Decimal("0"), None, "KRW")
