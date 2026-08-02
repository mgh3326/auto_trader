"""ROB-1036 — eligibility-aware read models.

``status == 'closed' AND brier_score IS NOT NULL`` is *not* an eligibility
predicate: it says the row was scored, not that it belongs in the cohort.  Every
builder here therefore demands an explicit contract version and an explicit
:class:`EligibilityPredicate`, and every result carries the
included/excluded/unidentifiable counts plus the reasons, so a caller can never
present a filtered cohort as if nothing had been dropped.

The partition helper is pure, so trade-performance / PnL consumers apply exactly
the same gate as calibration without re-deriving it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invalid_sample_eligibility.contract import (
    CalibrationEligibility,
    EligibilityContractError,
    EligibilityDecision,
    EligibilitySubject,
    EligibilitySubjectKind,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
)
from app.services.invalid_sample_eligibility.service import (
    InvalidSampleEligibilityService,
)
from app.services.trade_journal.forecast_service import (
    aggregate_calibration_rows,
    list_scored_forecasts_for_calibration,
)


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

_UNIDENTIFIABLE_MEMBERS = {
    ForecastOutcomeObservability.UNIDENTIFIABLE,
    CalibrationEligibility.UNIDENTIFIABLE,
    TradePerformanceEligibility.UNIDENTIFIABLE,
    OperationalReliabilityEligibility.UNIDENTIFIABLE,
}


@dataclass(frozen=True, slots=True)
class EligibilityPredicate:
    """One domain, one explicit admitted set, one contract version.

    ``admitted`` must be members of that domain's own enum — a calibration
    predicate cannot be satisfied by a trade-performance value.
    """

    contract_version: str
    domain: EligibilityDomain
    admitted: frozenset[StrEnum]

    def __post_init__(self) -> None:
        if not isinstance(self.domain, EligibilityDomain):
            raise TypeError("domain must be an EligibilityDomain")
        if not self.contract_version.strip():
            raise EligibilityContractError(
                "missing_contract_version", "contract_version must be non-empty"
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

    def value_of(self, decision: EligibilityDecision) -> StrEnum:
        return getattr(decision, self.domain.value)

    def admits(self, decision: EligibilityDecision) -> bool:
        if decision.contract_version != self.contract_version:
            return False
        return self.value_of(decision) in self.admitted


@dataclass(frozen=True, slots=True)
class EligibilityPartition:
    """Admitted rows plus a full account of what was held back and why."""

    included: list[Any]
    excluded: list[Any]
    unidentifiable: list[Any]
    contract_version: str
    domain: EligibilityDomain
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "included": len(self.included),
            "excluded": len(self.excluded),
            "unidentifiable": len(self.unidentifiable),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "eligibility_domain": self.domain.value,
            "eligibility_counts": self.counts,
            "eligibility_reasons": dict(self.reasons),
        }


def partition_by_eligibility(
    items: Sequence[tuple[Any, EligibilityDecision]],
    *,
    predicate: EligibilityPredicate,
) -> EligibilityPartition:
    """Split ``(row, decision)`` pairs into included / excluded / unidentifiable."""

    included: list[Any] = []
    excluded: list[Any] = []
    unidentifiable: list[Any] = []
    reasons: Counter[str] = Counter()

    for row, decision in items:
        value = predicate.value_of(decision)
        if decision.contract_version != predicate.contract_version:
            unidentifiable.append(row)
            reasons[f"contract_version_mismatch:{decision.contract_version}"] += 1
            continue
        if value in _UNIDENTIFIABLE_MEMBERS:
            unidentifiable.append(row)
            reasons[str(value)] += 1
            continue
        if value in predicate.admitted:
            included.append(row)
            continue
        excluded.append(row)
        reasons[str(value)] += 1

    return EligibilityPartition(
        included=included,
        excluded=excluded,
        unidentifiable=unidentifiable,
        contract_version=predicate.contract_version,
        domain=predicate.domain,
        reasons=dict(reasons),
    )


async def build_eligible_forecast_calibration_aggregate(
    db: AsyncSession,
    *,
    contract_version: str,
    predicate: EligibilityPredicate,
    group_by: str = "created_by",
    created_by: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Calibration over the *eligible* cohort only.

    ``contract_version`` and ``predicate`` are required keyword arguments with no
    defaults: a caller cannot obtain a cohort without stating, on the record,
    which contract and which admitted values it is asking for.
    """

    if predicate.contract_version != contract_version:
        raise EligibilityContractError(
            "predicate_contract_version_mismatch",
            (
                f"predicate is bound to {predicate.contract_version!r} but the "
                f"cohort was requested for {contract_version!r}"
            ),
        )
    if predicate.domain is not EligibilityDomain.CALIBRATION:
        raise EligibilityContractError(
            "wrong_predicate_domain",
            "a calibration cohort requires an EligibilityDomain.CALIBRATION predicate",
        )

    rows = await list_scored_forecasts_for_calibration(
        db,
        created_by=created_by,
        symbol=symbol,
        instrument_type=instrument_type,
        days=days,
        apply_eligibility_exclusion=False,
    )
    service = InvalidSampleEligibilityService(db)
    subjects = [
        EligibilitySubject(
            kind=EligibilitySubjectKind.FORECAST, ref=str(row.forecast_id)
        )
        for row in rows
    ]
    decisions = await service.get_decisions(subjects, contract_version=contract_version)
    partition = partition_by_eligibility(
        [
            (
                row,
                decisions[
                    EligibilitySubject(
                        kind=EligibilitySubjectKind.FORECAST, ref=str(row.forecast_id)
                    )
                ],
            )
            for row in rows
        ],
        predicate=predicate,
    )
    aggregate = aggregate_calibration_rows(partition.included, group_by=group_by)
    return {**aggregate, **partition.as_dict()}


__all__ = [
    "EligibilityDomain",
    "EligibilityPartition",
    "EligibilityPredicate",
    "build_eligible_forecast_calibration_aggregate",
    "partition_by_eligibility",
]
