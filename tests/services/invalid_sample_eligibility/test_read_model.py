"""ROB-1036 §4.3-3/10 — the read model requires an explicit predicate.

Offline: the partition helper is pure, so the eligibility gate is provable
without a database.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest

from app.services.invalid_sample_eligibility.contract import (
    CONTRACT_VERSION,
    CalibrationEligibility,
    EligibilityContractError,
    EligibilityDecision,
    EligibilitySubject,
    EligibilitySubjectKind,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
    canonical_evidence_hash,
    unidentifiable_decision,
)
from app.services.invalid_sample_eligibility.read_model import (
    EligibilityDomain,
    EligibilityPredicate,
    build_eligible_forecast_calibration_aggregate,
    partition_by_eligibility,
)

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class FakeForecastRow:
    """Only what the calibration aggregate reads."""

    forecast_id: str
    status: str = "closed"
    brier_score: float | None = 0.09


def _subject(ref: str) -> EligibilitySubject:
    return EligibilitySubject(kind=EligibilitySubjectKind.FORECAST, ref=ref)


def _decision(
    ref: str,
    *,
    calibration: CalibrationEligibility,
    trade: TradePerformanceEligibility = TradePerformanceEligibility.INCLUDE,
    contract_version: str = CONTRACT_VERSION,
) -> EligibilityDecision:
    return EligibilityDecision(
        subject=_subject(ref),
        contract_version=contract_version,
        revision_no=1,
        supersedes_revision_no=None,
        forecast_outcome_observability=ForecastOutcomeObservability.OBSERVABLE,
        calibration_eligibility=calibration,
        trade_performance_eligibility=trade,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason="test",
        evidence_hash=canonical_evidence_hash({}),
    )


CALIBRATION_PREDICATE = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.CALIBRATION,
    admitted=frozenset({CalibrationEligibility.INCLUDE}),
)
TRADE_PREDICATE = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.TRADE_PERFORMANCE,
    admitted=frozenset({TradePerformanceEligibility.INCLUDE}),
)


# --- §4.3-3: excluded samples never re-enter -------------------------------


def test_excluded_and_unidentifiable_rows_are_partitioned_out() -> None:
    included_row = FakeForecastRow("f-included")
    excluded_row = FakeForecastRow("f-excluded")
    unknown_row = FakeForecastRow("f-unknown")

    partition = partition_by_eligibility(
        [
            (
                included_row,
                _decision("f-included", calibration=CalibrationEligibility.INCLUDE),
            ),
            (
                excluded_row,
                _decision("f-excluded", calibration=CalibrationEligibility.EXCLUDE),
            ),
            (unknown_row, unidentifiable_decision(_subject("f-unknown"))),
        ],
        predicate=CALIBRATION_PREDICATE,
    )

    assert partition.included == [included_row]
    assert partition.excluded == [excluded_row]
    assert partition.unidentifiable == [unknown_row]
    assert partition.counts == {"included": 1, "excluded": 1, "unidentifiable": 1}


def test_partition_preserves_reasons_and_contract_version() -> None:
    partition = partition_by_eligibility(
        [
            (
                FakeForecastRow("f-excluded"),
                _decision("f-excluded", calibration=CalibrationEligibility.EXCLUDE),
            )
        ],
        predicate=CALIBRATION_PREDICATE,
    )
    payload = partition.as_dict()
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["eligibility_domain"] == "calibration_eligibility"
    assert payload["eligibility_counts"]["excluded"] == 1
    assert payload["eligibility_reasons"] == {CalibrationEligibility.EXCLUDE.value: 1}


def test_trade_performance_uses_its_own_domain_independently() -> None:
    """Excluded from trade performance, still included in calibration."""

    row = FakeForecastRow("f-1")
    decision = _decision(
        "f-1",
        calibration=CalibrationEligibility.INCLUDE,
        trade=TradePerformanceEligibility.EXCLUDE,
    )

    calibration = partition_by_eligibility(
        [(row, decision)], predicate=CALIBRATION_PREDICATE
    )
    trade = partition_by_eligibility([(row, decision)], predicate=TRADE_PREDICATE)

    assert calibration.included == [row]
    assert trade.excluded == [row]


def test_uber_shaped_decision_is_out_of_both_aggregates() -> None:
    row = FakeForecastRow("f-uber")
    decision = _decision(
        "f-uber",
        calibration=CalibrationEligibility.EXCLUDE,
        trade=TradePerformanceEligibility.EXCLUDE,
    )
    assert (
        partition_by_eligibility(
            [(row, decision)], predicate=CALIBRATION_PREDICATE
        ).included
        == []
    )
    assert (
        partition_by_eligibility([(row, decision)], predicate=TRADE_PREDICATE).included
        == []
    )


def test_decision_under_another_contract_version_is_unidentifiable() -> None:
    row = FakeForecastRow("f-legacy")
    partition = partition_by_eligibility(
        [
            (
                row,
                _decision(
                    "f-legacy",
                    calibration=CalibrationEligibility.INCLUDE,
                    contract_version="legacy-contract.v0",
                ),
            )
        ],
        predicate=CALIBRATION_PREDICATE,
    )
    assert partition.unidentifiable == [row]
    assert partition.included == []


# --- §4.3-10: the mutant must fail ----------------------------------------


def test_removing_the_eligibility_filter_readmits_the_excluded_row() -> None:
    """Mutation evidence: without the predicate gate, the exclusion vanishes.

    The unfiltered pass-through is what a "just aggregate everything closed and
    scored" implementation does. If this assertion ever matched the filtered
    result, the filter would be doing nothing and the guard tests above would be
    self-fulfilling.
    """

    row = FakeForecastRow("f-uber")
    pairs = [(row, _decision("f-uber", calibration=CalibrationEligibility.EXCLUDE))]

    filtered = partition_by_eligibility(pairs, predicate=CALIBRATION_PREDICATE)

    def mutant_partition(items, *, predicate):  # filter removed
        return [row for row, _decision in items]

    mutated = mutant_partition(pairs, predicate=CALIBRATION_PREDICATE)

    assert filtered.included == []
    assert mutated == [row]
    assert filtered.included != mutated


def test_status_closed_plus_brier_alone_is_not_an_eligibility_predicate() -> None:
    """A scored row with no decision is still not admitted."""

    row = FakeForecastRow("f-scored", status="closed", brier_score=0.04)
    partition = partition_by_eligibility(
        [(row, unidentifiable_decision(_subject("f-scored")))],
        predicate=CALIBRATION_PREDICATE,
    )
    assert partition.included == []
    assert partition.unidentifiable == [row]


# --- predicate hygiene ----------------------------------------------------


def test_contract_version_and_predicate_are_required_keyword_arguments() -> None:
    signature = inspect.signature(build_eligible_forecast_calibration_aggregate)
    for name in ("contract_version", "predicate"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def test_predicate_rejects_a_cross_domain_admitted_value() -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        EligibilityPredicate(
            contract_version=CONTRACT_VERSION,
            domain=EligibilityDomain.CALIBRATION,
            admitted=frozenset({TradePerformanceEligibility.INCLUDE}),
        )
    assert excinfo.value.code == "cross_domain_predicate"


def test_predicate_rejects_an_empty_admitted_set() -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        EligibilityPredicate(
            contract_version=CONTRACT_VERSION,
            domain=EligibilityDomain.CALIBRATION,
            admitted=frozenset(),
        )
    assert excinfo.value.code == "empty_admitted_set"


def test_predicate_rejects_a_blank_contract_version() -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        EligibilityPredicate(
            contract_version="  ",
            domain=EligibilityDomain.CALIBRATION,
            admitted=frozenset({CalibrationEligibility.INCLUDE}),
        )
    assert excinfo.value.code == "missing_contract_version"
