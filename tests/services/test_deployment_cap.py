"""§176차 — the advisory dynamic deployment cap.

The reference numbers are the 2026-09-07 measured position (framework-v0 §2 /
cash-active-week1-plan §d): 14,925,048 KRW broker orderable plus 7,800,000 KRW
parking, and a planned first-wave deployment of 9,433,468 KRW.
"""

from decimal import Decimal

import pytest

from app.services.deployment_cap import (
    PARKING_SOURCE_ABSENT,
    PARKING_SOURCE_MANUAL_CASH,
    PARKING_SOURCE_STALE,
    STATUS_EXCEEDS,
    STATUS_UNAVAILABLE,
    STATUS_WITHIN,
    DeploymentCapPolicyError,
    evaluate_deployment_cap,
    load_deployment_cap_policy,
    resolve_parking_balance_krw,
)
from app.services.trading_policy_service import load_trading_policy


def test_measured_2026_09_07_position_is_within_cap():
    result = evaluate_deployment_cap(
        broker_orderable_total_krw=14_925_048,
        parking_balance_krw=7_800_000,
        parking_balance_source=PARKING_SOURCE_MANUAL_CASH,
        planned_new_deployment_krw=9_433_468,
    )

    assert result["denominator_krw"] == "22725048"
    # 45% of 22,725,048 = 10,226,271.6 -> 10,226,272 at whole KRW.
    assert result["cap_krw"] == "10226272"
    assert result["status"] == STATUS_WITHIN
    assert result["headroom_krw"] == "792804"
    assert result["cap_is_lower_bound"] is False
    assert "warning" not in result


def test_exceeding_the_cap_warns_and_still_does_not_block():
    result = evaluate_deployment_cap(
        broker_orderable_total_krw=14_925_048,
        parking_balance_krw=7_800_000,
        parking_balance_source=PARKING_SOURCE_MANUAL_CASH,
        planned_new_deployment_krw=12_000_000,
    )

    assert result["status"] == STATUS_EXCEEDS
    assert result["excess_krw"] == "1773728"
    assert result["blocks_proposal"] is False
    assert "차단이 아닙니다" in result["warning"]


def test_existing_deployment_is_never_an_input():
    """The forward-only clause, asserted as an API property.

    ``evaluate_deployment_cap`` has no parameter for already-deployed capital,
    so there is no way for a caller to make an existing deployment breach this
    cap even by accident. A filling order shrinks the denominator, and that
    must never turn into a retroactive violation.
    """

    result = evaluate_deployment_cap(
        broker_orderable_total_krw=1_000_000,
        planned_new_deployment_krw=100_000,
    )

    assert result["evaluated_at"] == "new_deployment_only"
    assert result["retroactive_violation"] is False
    with pytest.raises(TypeError):
        evaluate_deployment_cap(
            broker_orderable_total_krw=1_000_000,
            existing_deployed_krw=9_000_000,  # type: ignore[call-arg]
        )


def test_missing_parking_reads_as_zero_and_is_declared_a_lower_bound():
    result = evaluate_deployment_cap(
        broker_orderable_total_krw=14_925_048,
        planned_new_deployment_krw=6_000_000,
    )

    assert result["parking_balance_krw"] == "0"
    assert result["parking_balance_source"] == PARKING_SOURCE_ABSENT
    assert result["cap_is_lower_bound"] is True
    # 45% of the broker term alone.
    assert result["cap_krw"] == "6716272"


@pytest.mark.parametrize(
    ("manual_cash", "expected_amount", "expected_source"),
    [
        (None, Decimal("0"), PARKING_SOURCE_ABSENT),
        ({}, Decimal("0"), PARKING_SOURCE_ABSENT),
        ({"amount": 0.0}, Decimal("0"), PARKING_SOURCE_ABSENT),
        ({"amount": -5.0}, Decimal("0"), PARKING_SOURCE_ABSENT),
        ({"amount": "not a number"}, Decimal("0"), PARKING_SOURCE_ABSENT),
        (
            {"amount": 7_800_000.0, "stale_warning": True},
            Decimal("0"),
            PARKING_SOURCE_STALE,
        ),
        (
            {"amount": 7_800_000.0, "stale_warning": False},
            Decimal("7800000"),
            PARKING_SOURCE_MANUAL_CASH,
        ),
    ],
)
def test_parking_provenance_never_estimates(
    manual_cash, expected_amount, expected_source
):
    """Stale is the case where estimating would be most tempting; it reads 0."""

    amount, source = resolve_parking_balance_krw(manual_cash)

    assert amount == expected_amount
    assert source == expected_source


def test_unreadable_broker_total_is_unavailable_not_zero():
    """A failed balance read must not produce a cap of 0 and warn on everything."""

    result = evaluate_deployment_cap(
        broker_orderable_total_krw=None,
        planned_new_deployment_krw=1_000_000,
    )

    assert result["status"] == STATUS_UNAVAILABLE
    assert result["reason"] == "broker_orderable_total_unavailable"
    assert "cap_krw" not in result


def test_cap_without_a_planned_amount_reports_the_cap_and_says_why_no_verdict():
    result = evaluate_deployment_cap(broker_orderable_total_krw=10_000_000)

    assert result["cap_krw"] == "4500000"
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["reason"] == "planned_new_deployment_not_supplied"


def test_coefficient_comes_from_the_policy_not_a_literal():
    document = load_trading_policy()
    contract = load_deployment_cap_policy(document)

    tier = [
        tier
        for tier in document.decision_rules["buy.deployment_cap"].tiers
        if tier.id == "deployment_cap"
    ][0]
    assert contract.coefficient_pct == Decimal(str(tier.conditions["coefficient_pct"]))
    assert contract.blocks_proposal is False
    assert contract.retroactive_violation is False


def test_missing_policy_rule_fails_closed_rather_than_assuming_45():
    document = load_trading_policy()
    stripped = document.model_copy(
        update={
            "decision_rules": {
                key: rule
                for key, rule in document.decision_rules.items()
                if key != "buy.deployment_cap"
            }
        }
    )

    with pytest.raises(DeploymentCapPolicyError):
        load_deployment_cap_policy(stripped)

    # ...and the evaluator turns that into an advisory, never into a number.
    result = evaluate_deployment_cap(
        broker_orderable_total_krw=10_000_000,
        planned_new_deployment_krw=1_000_000,
        policy=stripped,
    )
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["reason"] == "policy_unavailable"
