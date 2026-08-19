from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.funding_advisory.contracts import (
    FundingAssessment,
    FundingCandidateEvent,
    FundingRoute,
    PassedNonFundingGateEvidence,
)
from app.services.funding_advisory.ranking import (
    build_reference_combination,
    compare_routes,
    dominates,
)

NOW = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)


def evidence(**overrides) -> PassedNonFundingGateEvidence:
    payload = {
        "owner_user_id": 11,
        "source_kind": "upbit_live_candidate",
        "source_candidate_id": "candidate-1",
        "gate_name": "crypto_non_funding_gate",
        "gate_version": "crypto-gate.v1",
        "gate_verdict": "passed",
        "gate_evaluated_at": NOW,
        "valid_until": NOW + timedelta(hours=1),
        "market": "crypto",
        "target_account_mode": "upbit",
        "broker_account_id": "upbit-primary",
        "currency": "KRW",
        "symbol": "KRW-BTC",
        "side": "buy",
        "order_type": "limit",
        "quantity": "0.001",
        "price_reference": "100000000",
        "blocking_reasons": [],
        "non_funding_checks": [
            {
                "check_id": "candidate_quality",
                "check_version": "v3",
                "verdict": "passed",
                "evaluated_at": NOW,
            }
        ],
        "upstream_priority": "source-rank-1",
    }
    payload.update(overrides)
    return PassedNonFundingGateEvidence.issue(**payload)


def assessment(**overrides) -> FundingAssessment:
    payload = {
        "required_cash": Decimal("100000"),
        "target_buying_power": Decimal("40000"),
        "other_pending_required": Decimal("20000"),
        "reserved_cash": Decimal("10000"),
        "currency": "KRW",
        "observed_at": NOW,
        "valid_until": NOW + timedelta(minutes=30),
        "source": "upbit_accounts_free_krw",
    }
    payload.update(overrides)
    return FundingAssessment(**payload)


def route(
    route_id: str,
    *,
    amount: str,
    cost: str,
    eta: int,
    impact: str,
    reversibility: str,
) -> FundingRoute:
    return FundingRoute.model_validate(
        {
            "route_id": route_id,
            "label": route_id,
            "amount_status": "known",
            "route_fundable_amount": Decimal(amount),
            "counted_fundable_amount": Decimal(amount),
            "confidence": "broker_authoritative",
            "source_as_of": NOW,
            "deadline_status": "met",
            "explicit_cost": Decimal(cost),
            "eta_minutes": eta,
            "realized_impact": Decimal(impact),
            "reversibility": reversibility,
            "eligibility": "eligible",
            "reason_codes": [],
        }
    )


def test_gate_version_is_contract_version_and_hash_is_per_evaluation() -> None:
    issued = evidence()
    assert issued.gate_version == "crypto-gate.v1"
    assert len(issued.evidence_hash) == 64

    with pytest.raises(ValidationError, match="contract/schema version"):
        evidence(gate_version="a" * 64)


def test_evidence_hash_tamper_is_rejected() -> None:
    issued = evidence()
    payload = issued.model_dump(mode="json")
    payload["source_candidate_id"] = "candidate-tampered"
    with pytest.raises(ValidationError, match="evidence_hash"):
        PassedNonFundingGateEvidence.model_validate(payload)


def test_mock_shadow_and_paper_accounts_are_not_valid_evidence() -> None:
    with pytest.raises(ValidationError):
        evidence(target_account_mode="upbit_shadow")


def test_assessment_is_bound_to_evidence_currency_and_lifetime() -> None:
    with pytest.raises(ValidationError, match="currency mismatch"):
        FundingCandidateEvent(
            evidence=evidence(), assessment=assessment(currency="USD")
        )
    with pytest.raises(ValidationError, match="cannot outlive"):
        FundingCandidateEvent(
            evidence=evidence(valid_until=NOW + timedelta(minutes=10)),
            assessment=assessment(valid_until=NOW + timedelta(minutes=20)),
        )


def test_multi_axis_dominance_has_no_hidden_scalar_score() -> None:
    dominant = route(
        "EXTERNAL_PARKING_KRW",
        amount="70000",
        cost="10",
        eta=5,
        impact="0",
        reversibility="reversible",
    )
    dominated = route(
        "USD_CONVERSION",
        amount="60000",
        cost="20",
        eta=10,
        impact="1",
        reversibility="conditional",
    )
    assert dominates(dominant, dominated) is True
    compared = compare_routes([dominant, dominated])
    assert compared[0].comparison == "preferred"
    assert compared[1].comparison == "dominated"


def test_cheap_slow_irreversible_route_is_not_auto_selected() -> None:
    cheap_slow = route(
        "LOSS_CUT_ROTATION",
        amount="60000",
        cost="1",
        eta=120,
        impact="5000",
        reversibility="irreversible",
    )
    dear_fast = route(
        "USD_CONVERSION",
        amount="60000",
        cost="100",
        eta=5,
        impact="0",
        reversibility="reversible",
    )
    compared = compare_routes([cheap_slow, dear_fast])
    assert {item.comparison for item in compared} == {"situation_dependent"}

    scenario = build_reference_combination(compared, shortfall=Decimal("60000"))
    assert scenario["scenario_kind"] == "reference_only"
    assert scenario["selected"] is False
    assert scenario["selection_basis"] == "operator_decision_required_multi_axis"
