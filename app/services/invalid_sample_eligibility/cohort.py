"""ROB-1036 D-1 — cohort predicates for eligibility-aware aggregates.

Pure: stdlib + :mod:`contract` only. It deliberately imports no aggregate
implementation, so an aggregate module can require a predicate without a cycle.

Operator decision D-1 (2026-08-02) selected **option ②**: every calibration call
must state its contract version and cohort, and the default cohort admits
``UNIDENTIFIABLE`` alongside ``INCLUDE`` so the existing calibration surfaces keep
working while the undecided population is made visible rather than silent.

.. _rob1036-d1-termination:

Termination condition
---------------------
:data:`COMPATIBILITY_CALIBRATION_COHORT` is a **transitional** stage, not the end
state. It ends when both hold:

1. every forecast in the scored population carries an explicit eligibility
   decision recorded through ``InvalidSampleEligibilityService`` — recorded by an
   operator, never by an automatic historical backfill (§4.2-4); and
2. the reported ``unidentifiable`` count for the cohorts an operator relies on has
   been 0 for a full review cycle.

At that point the call sites move to :data:`DECIDED_ONLY_CALIBRATION_COHORT` and
the compatibility cohort is deleted. Because option ② is a superset of the
end state — same machinery, wider admitted set — that promotion is a one-line
change per call site and closes no door.

Every result stamped with a cohort keeps its provenance, so numbers produced
during this stage stay distinguishable from post-promotion numbers and a
before/after comparison report remains possible.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.services.invalid_sample_eligibility.contract import (
    CONTRACT_VERSION,
    CalibrationEligibility,
    EligibilityContractError,
    EligibilityDecision,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
)

#: Marks a result as produced during the D-1 option-② compatibility stage.
COMPATIBILITY_STAGE = "rob-1036-d1-option-2-compatibility"

#: Marks a result as produced by the option-① end-state cohort.
DECIDED_ONLY_STAGE = "rob-1036-decided-only"


class EligibilityDomain(StrEnum):
    """Which of the four validity questions a predicate asks."""

    FORECAST_OUTCOME_OBSERVABILITY = "forecast_outcome_observability"
    CALIBRATION = "calibration_eligibility"
    TRADE_PERFORMANCE = "trade_performance_eligibility"
    OPERATIONAL_RELIABILITY = "operational_reliability_eligibility"


_DOMAIN_ENUMS: dict[EligibilityDomain, type[StrEnum]] = {
    EligibilityDomain.FORECAST_OUTCOME_OBSERVABILITY: ForecastOutcomeObservability,
    EligibilityDomain.CALIBRATION: CalibrationEligibility,
    EligibilityDomain.TRADE_PERFORMANCE: TradePerformanceEligibility,
    EligibilityDomain.OPERATIONAL_RELIABILITY: OperationalReliabilityEligibility,
}

_INCLUDE_MEMBERS = frozenset(
    {
        ForecastOutcomeObservability.OBSERVABLE,
        CalibrationEligibility.INCLUDE,
        TradePerformanceEligibility.INCLUDE,
        OperationalReliabilityEligibility.INCLUDE,
    }
)
_EXCLUDE_MEMBERS = frozenset(
    {
        ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE,
        CalibrationEligibility.EXCLUDE,
        TradePerformanceEligibility.EXCLUDE,
        OperationalReliabilityEligibility.EXCLUDE,
    }
)
_UNIDENTIFIABLE_MEMBERS = frozenset(
    {
        ForecastOutcomeObservability.UNIDENTIFIABLE,
        CalibrationEligibility.UNIDENTIFIABLE,
        TradePerformanceEligibility.UNIDENTIFIABLE,
        OperationalReliabilityEligibility.UNIDENTIFIABLE,
    }
)


class EligibilityBucket(StrEnum):
    """How one subject was classified before the admitted set is consulted."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNIDENTIFIABLE = "unidentifiable"


@dataclass(frozen=True, slots=True)
class EligibilityPredicate:
    """One domain, one named cohort, one explicit admitted set, one version.

    ``label`` is stamped onto every result built with this predicate, so a stored
    or reported number stays attributable to the cohort definition that produced
    it (D-1 provenance requirement).
    """

    contract_version: str
    domain: EligibilityDomain
    admitted: frozenset[StrEnum]
    label: str
    stage: str = COMPATIBILITY_STAGE

    def __post_init__(self) -> None:
        if not isinstance(self.domain, EligibilityDomain):
            raise TypeError("domain must be an EligibilityDomain")
        if not self.contract_version.strip():
            raise EligibilityContractError(
                "missing_contract_version", "contract_version must be non-empty"
            )
        if not self.label.strip():
            raise EligibilityContractError(
                "missing_cohort_label",
                "a cohort must be named so its results stay attributable",
            )
        if not self.admitted:
            raise EligibilityContractError(
                "empty_admitted_set",
                "an eligibility predicate must admit at least one value",
            )
        expected = _DOMAIN_ENUMS[self.domain]
        for member in self.admitted:
            if not isinstance(member, expected):
                raise EligibilityContractError(
                    "cross_domain_predicate",
                    f"{member!r} is not a {expected.__name__} value",
                )

    @property
    def admits_unidentifiable(self) -> bool:
        return bool(self.admitted & _UNIDENTIFIABLE_MEMBERS)

    def value_of(self, decision: EligibilityDecision) -> StrEnum:
        return getattr(decision, self.domain.value)

    def bucket_of(self, decision: EligibilityDecision) -> EligibilityBucket:
        """Classify a decision independently of what this cohort admits.

        An explicit exclusion is an exclusion under **any** contract version — a
        version bump must never resurrect an excluded sample. An inclusion is only
        trusted under the matching version; otherwise it is unidentifiable.
        """

        value = self.value_of(decision)
        if value in _EXCLUDE_MEMBERS:
            return EligibilityBucket.EXCLUDED
        if decision.contract_version != self.contract_version:
            return EligibilityBucket.UNIDENTIFIABLE
        if value in _INCLUDE_MEMBERS:
            return EligibilityBucket.INCLUDED
        return EligibilityBucket.UNIDENTIFIABLE

    def admits(self, decision: EligibilityDecision) -> bool:
        return self.value_of(decision) in self.admitted


#: D-1 option ② — the transitional default. Admits decided inclusions *and*
#: undecided samples, and reports the two quantities separately so the undecided
#: contamination is visible to the caller instead of silently folded in.
#: See :ref:`the termination condition <rob1036-d1-termination>`.
COMPATIBILITY_CALIBRATION_COHORT = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.CALIBRATION,
    admitted=frozenset(
        {CalibrationEligibility.INCLUDE, CalibrationEligibility.UNIDENTIFIABLE}
    ),
    label="calibration-compatibility",
    stage=COMPATIBILITY_STAGE,
)

#: The option-① end state: decided inclusions only. Available now so callers that
#: want the fully-decided cohort can ask for it, and so the promotion is a
#: one-line swap rather than a redesign.
DECIDED_ONLY_CALIBRATION_COHORT = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.CALIBRATION,
    admitted=frozenset({CalibrationEligibility.INCLUDE}),
    label="calibration-decided-only",
    stage=DECIDED_ONLY_STAGE,
)

#: Trade-performance counterpart of the end-state cohort (ROB-1036 B2 wiring).
DECIDED_ONLY_TRADE_PERFORMANCE_COHORT = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.TRADE_PERFORMANCE,
    admitted=frozenset({TradePerformanceEligibility.INCLUDE}),
    label="trade-performance-decided-only",
    stage=DECIDED_ONLY_STAGE,
)


@dataclass(frozen=True, slots=True)
class EligibilityPartition:
    """Classification buckets, the admitted cohort, and the cohort's provenance.

    ``included`` / ``excluded`` / ``unidentifiable`` are **classification**
    buckets and are always reported separately — they are never summed into a
    single "eligible" number. ``admitted`` is the cohort the aggregate actually
    runs over, which under the compatibility cohort is
    ``included + unidentifiable``.
    """

    included: list[Any]
    excluded: list[Any]
    unidentifiable: list[Any]
    admitted: list[Any]
    contract_version: str
    domain: EligibilityDomain
    cohort_label: str
    cohort_stage: str
    cohort_admits: tuple[str, ...]
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        """The three classification counts, kept separate on purpose."""

        return {
            "included": len(self.included),
            "excluded": len(self.excluded),
            "unidentifiable": len(self.unidentifiable),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "eligibility_domain": self.domain.value,
            "eligibility_cohort": self.cohort_label,
            "eligibility_stage": self.cohort_stage,
            "eligibility_cohort_admits": list(self.cohort_admits),
            "eligibility_counts": self.counts,
            "eligibility_admitted_count": len(self.admitted),
            "eligibility_reasons": dict(self.reasons),
        }


def partition_by_eligibility(
    items: Sequence[tuple[Any, EligibilityDecision]],
    *,
    predicate: EligibilityPredicate,
) -> EligibilityPartition:
    """Classify ``(row, decision)`` pairs and build the admitted cohort.

    Classification is independent of the admitted set, so the three counts mean
    the same thing under every cohort and a compatibility-stage number can be
    compared against a post-promotion number.
    """

    included: list[Any] = []
    excluded: list[Any] = []
    unidentifiable: list[Any] = []
    admitted: list[Any] = []
    reasons: Counter[str] = Counter()

    for row, decision in items:
        bucket = predicate.bucket_of(decision)
        value = predicate.value_of(decision)
        if bucket is EligibilityBucket.EXCLUDED:
            excluded.append(row)
            reasons[str(value)] += 1
        elif bucket is EligibilityBucket.UNIDENTIFIABLE:
            unidentifiable.append(row)
            if decision.contract_version != predicate.contract_version:
                reasons[f"contract_version_mismatch:{decision.contract_version}"] += 1
            else:
                reasons[str(value)] += 1
        else:
            included.append(row)
        if value in predicate.admitted:
            admitted.append(row)

    return EligibilityPartition(
        included=included,
        excluded=excluded,
        unidentifiable=unidentifiable,
        admitted=admitted,
        contract_version=predicate.contract_version,
        domain=predicate.domain,
        cohort_label=predicate.label,
        cohort_stage=predicate.stage,
        cohort_admits=tuple(sorted(str(member) for member in predicate.admitted)),
        reasons=dict(reasons),
    )


__all__ = [
    "COMPATIBILITY_CALIBRATION_COHORT",
    "COMPATIBILITY_STAGE",
    "DECIDED_ONLY_CALIBRATION_COHORT",
    "DECIDED_ONLY_STAGE",
    "DECIDED_ONLY_TRADE_PERFORMANCE_COHORT",
    "EligibilityBucket",
    "EligibilityDomain",
    "EligibilityPartition",
    "EligibilityPredicate",
    "partition_by_eligibility",
]
