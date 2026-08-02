"""ROB-1036 §4.3 — four-domain independence, revision chain, fail-closed default.

Offline: stdlib + the pure contract module only. No DB, broker, or network.
"""

from __future__ import annotations

import itertools

import pytest

from app.services.invalid_sample_eligibility.contract import (
    CONTRACT_VERSION,
    ELIGIBILITY_DOMAIN_ENUMS,
    CalibrationEligibility,
    EligibilityContractError,
    EligibilityDecision,
    EligibilitySubject,
    EligibilitySubjectKind,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
    canonical_evidence_hash,
    latest_decision,
    unidentifiable_decision,
    validate_revision_chain,
)

pytestmark = pytest.mark.unit

SUBJECT = EligibilitySubject(
    kind=EligibilitySubjectKind.FORECAST, ref="11111111-2222-3333-4444-555555555555"
)


def _decision(
    *,
    revision_no: int = 1,
    observability: ForecastOutcomeObservability = (
        ForecastOutcomeObservability.OBSERVABLE
    ),
    calibration: CalibrationEligibility = CalibrationEligibility.INCLUDE,
    trade: TradePerformanceEligibility = TradePerformanceEligibility.INCLUDE,
    operational: OperationalReliabilityEligibility = (
        OperationalReliabilityEligibility.INCLUDE
    ),
    supersedes: int | None = -1,
    subject: EligibilitySubject = SUBJECT,
    contract_version: str = CONTRACT_VERSION,
) -> EligibilityDecision:
    if supersedes == -1:
        supersedes = None if revision_no == 1 else revision_no - 1
    return EligibilityDecision(
        subject=subject,
        contract_version=contract_version,
        revision_no=revision_no,
        supersedes_revision_no=supersedes,
        forecast_outcome_observability=observability,
        calibration_eligibility=calibration,
        trade_performance_eligibility=trade,
        operational_reliability_eligibility=operational,
        decision_reason="test",
        evidence_hash=canonical_evidence_hash({}),
    )


# --- §4.3-1: the four domains vary independently ---------------------------


def test_every_combination_of_the_four_domains_is_representable() -> None:
    """A domain is never inferable from another: all 81 combinations construct."""

    combinations = list(
        itertools.product(
            ForecastOutcomeObservability,
            CalibrationEligibility,
            TradePerformanceEligibility,
            OperationalReliabilityEligibility,
        )
    )
    assert len(combinations) == 3 * 3 * 3 * 3

    for observability, calibration, trade, operational in combinations:
        decision = _decision(
            observability=observability,
            calibration=calibration,
            trade=trade,
            operational=operational,
        )
        assert decision.forecast_outcome_observability is observability
        assert decision.calibration_eligibility is calibration
        assert decision.trade_performance_eligibility is trade
        assert decision.operational_reliability_eligibility is operational


def test_domain_enums_share_no_member_values() -> None:
    """Distinct value spaces make a cross-domain assignment detectable."""

    seen: set[str] = set()
    for enum_type in ELIGIBILITY_DOMAIN_ENUMS:
        values = {member.value for member in enum_type}
        assert not (values & seen), (
            f"{enum_type.__name__} reuses another domain's value"
        )
        seen |= values


def test_decision_exposes_no_collapsed_validity_bit() -> None:
    """No ``is_valid`` / ``eligible`` / ``success`` aggregate may exist."""

    forbidden = {
        "is_valid",
        "valid",
        "eligible",
        "is_eligible",
        "success",
        "ok",
        "overall",
        "overall_eligibility",
    }
    exposed = set(dir(EligibilityDecision))
    assert not (exposed & forbidden)


@pytest.mark.parametrize(
    ("domain_value", "wrong_field"),
    [
        (CalibrationEligibility.EXCLUDE, "forecast_outcome_observability"),
        (TradePerformanceEligibility.EXCLUDE, "calibration_eligibility"),
        (OperationalReliabilityEligibility.EXCLUDE, "trade_performance_eligibility"),
        (
            ForecastOutcomeObservability.OBSERVABLE,
            "operational_reliability_eligibility",
        ),
    ],
)
def test_cross_domain_assignment_is_rejected(domain_value, wrong_field: str) -> None:
    kwargs = {
        "subject": SUBJECT,
        "contract_version": CONTRACT_VERSION,
        "revision_no": 1,
        "supersedes_revision_no": None,
        "forecast_outcome_observability": ForecastOutcomeObservability.OBSERVABLE,
        "calibration_eligibility": CalibrationEligibility.INCLUDE,
        "trade_performance_eligibility": TradePerformanceEligibility.INCLUDE,
        "operational_reliability_eligibility": (
            OperationalReliabilityEligibility.INCLUDE
        ),
        "decision_reason": "test",
        "evidence_hash": canonical_evidence_hash({}),
    }
    kwargs[wrong_field] = domain_value
    with pytest.raises(TypeError):
        EligibilityDecision(**kwargs)


# --- §4.3-2: an invalid sample does not discard the outcome record ---------


def test_uber_shaped_decision_keeps_the_outcome_record() -> None:
    """The UBER decision: excluded from scoring cohorts, outcome NOT discarded.

    Observability is *blocked pending evidence* — a hold, not a deletion — while
    calibration and trade performance are excluded.  Operational reliability
    stays included: the cleanup attempt is still a real operational event.
    """

    decision = _decision(
        observability=ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE,
        calibration=CalibrationEligibility.EXCLUDE,
        trade=TradePerformanceEligibility.EXCLUDE,
        operational=OperationalReliabilityEligibility.INCLUDE,
    )
    assert (
        decision.forecast_outcome_observability
        is not ForecastOutcomeObservability.UNIDENTIFIABLE
    )
    assert decision.calibration_eligibility is CalibrationEligibility.EXCLUDE
    assert decision.trade_performance_eligibility is TradePerformanceEligibility.EXCLUDE
    assert (
        decision.operational_reliability_eligibility
        is OperationalReliabilityEligibility.INCLUDE
    )
    # There is no contract-level operation that deletes or resolves an outcome.
    assert not hasattr(decision, "discard")
    assert not hasattr(decision, "resolve")


# --- §4.3-4: missing decision / legacy row is UNIDENTIFIABLE ---------------


def test_missing_decision_is_unidentifiable_in_all_four_domains() -> None:
    decision = unidentifiable_decision(SUBJECT)
    assert (
        decision.forecast_outcome_observability
        is ForecastOutcomeObservability.UNIDENTIFIABLE
    )
    assert decision.calibration_eligibility is CalibrationEligibility.UNIDENTIFIABLE
    assert (
        decision.trade_performance_eligibility
        is TradePerformanceEligibility.UNIDENTIFIABLE
    )
    assert (
        decision.operational_reliability_eligibility
        is OperationalReliabilityEligibility.UNIDENTIFIABLE
    )


def test_empty_chain_never_defaults_to_include() -> None:
    decision = latest_decision([], SUBJECT)
    for value in (
        decision.forecast_outcome_observability,
        decision.calibration_eligibility,
        decision.trade_performance_eligibility,
        decision.operational_reliability_eligibility,
    ):
        assert "include" not in value.value


# --- §4.3-5: revision chain gaps, branches, cycles -------------------------


def test_valid_chain_returns_highest_revision() -> None:
    chain = [
        _decision(revision_no=1, calibration=CalibrationEligibility.INCLUDE),
        _decision(revision_no=2, calibration=CalibrationEligibility.EXCLUDE),
    ]
    assert latest_decision(chain, SUBJECT).calibration_eligibility is (
        CalibrationEligibility.EXCLUDE
    )


def test_revision_gap_is_rejected() -> None:
    chain = [_decision(revision_no=1), _decision(revision_no=3)]
    with pytest.raises(EligibilityContractError) as excinfo:
        validate_revision_chain(chain)
    assert excinfo.value.code == "revision_chain_gap"


def test_branched_chain_is_rejected() -> None:
    chain = [
        _decision(revision_no=1),
        _decision(revision_no=2, calibration=CalibrationEligibility.EXCLUDE),
        _decision(revision_no=2, calibration=CalibrationEligibility.INCLUDE),
    ]
    with pytest.raises(EligibilityContractError) as excinfo:
        validate_revision_chain(chain)
    assert excinfo.value.code == "branched_revision_chain"


@pytest.mark.parametrize("supersedes", [1, 2, 3])
def test_self_or_forward_reference_is_unconstructable(supersedes: int) -> None:
    """A cycle cannot even be built: supersedes must be exactly revision - 1."""

    with pytest.raises(EligibilityContractError) as excinfo:
        _decision(revision_no=2, supersedes=supersedes if supersedes != 1 else 0)
    assert excinfo.value.code == "invalid_supersedes_revision_no"


def test_revision_one_must_not_supersede_anything() -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        _decision(revision_no=1, supersedes=0)
    assert excinfo.value.code == "invalid_supersedes_revision_no"


def test_chain_may_not_switch_contract_version() -> None:
    chain = [
        _decision(revision_no=1),
        _decision(revision_no=2, contract_version="some-other-contract.v9"),
    ]
    with pytest.raises(EligibilityContractError) as excinfo:
        validate_revision_chain(chain)
    assert excinfo.value.code == "contract_version_switch"


def test_chain_may_not_mix_subjects() -> None:
    other = EligibilitySubject(
        kind=EligibilitySubjectKind.TRADE_LIFECYCLE, ref="corr-other"
    )
    chain = [_decision(revision_no=1), _decision(revision_no=2, subject=other)]
    with pytest.raises(EligibilityContractError) as excinfo:
        validate_revision_chain(chain)
    assert excinfo.value.code == "subject_mismatch"


def test_malformed_evidence_hash_is_rejected() -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        EligibilityDecision(
            subject=SUBJECT,
            contract_version=CONTRACT_VERSION,
            revision_no=1,
            supersedes_revision_no=None,
            forecast_outcome_observability=ForecastOutcomeObservability.OBSERVABLE,
            calibration_eligibility=CalibrationEligibility.INCLUDE,
            trade_performance_eligibility=TradePerformanceEligibility.INCLUDE,
            operational_reliability_eligibility=(
                OperationalReliabilityEligibility.INCLUDE
            ),
            decision_reason="test",
            evidence_hash="NOT-A-HASH",
        )
    assert excinfo.value.code == "invalid_evidence_hash"


def test_canonical_evidence_hash_is_key_order_independent() -> None:
    assert canonical_evidence_hash({"a": 1, "b": 2}) == canonical_evidence_hash(
        {"b": 2, "a": 1}
    )
    assert canonical_evidence_hash({"a": 1}) != canonical_evidence_hash({"a": 2})
