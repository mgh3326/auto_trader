"""ROB-1036 — ``uber-invalid-sample-eligibility.v1`` validity algebra.

A trade can be an invalid sample without the forecast attached to it being
worthless, and a forecast can be unusable for calibration while its operational
record still counts.  Those are four different questions, so this module keeps
four *separate* domains and deliberately offers no combined ``is_valid`` bit:

``ForecastOutcomeObservability``
    May the forecast outcome be observed/resolved at all?  Trade invalidity
    alone never discards the outcome record; the UBER decision is that the
    outcome stays but resolution is blocked until audit-grade provider evidence
    exists.
``CalibrationEligibility``
    Does the forecast enter the calibration primary cohort?
``TradePerformanceEligibility``
    Does the lifecycle enter trade-performance / PnL aggregates?
``OperationalReliabilityEligibility``
    Does the lifecycle count towards operational reliability?

The four enums are distinct types with distinct member sets, so a caller cannot
silently substitute one domain's decision for another's.

This module is pure: stdlib only, no DB, broker, network, or clock access.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Append-only contract version fixed by the operator decision.  A decision row
#: is only meaningful together with the version it was taken under.
CONTRACT_VERSION = "uber-invalid-sample-eligibility.v1"

#: Versions this build knows how to interpret.  An unknown version is never
#: coerced to the current one.
KNOWN_CONTRACT_VERSIONS = frozenset({CONTRACT_VERSION})


class EligibilityContractError(ValueError):
    """Raised when a decision or revision chain violates the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EligibilitySubjectKind(StrEnum):
    """What a decision is *about*.

    A forecast and the broker lifecycle that was supposed to realise it are
    separate subjects: one can be identifiable while the other is not.
    """

    FORECAST = "forecast"
    TRADE_LIFECYCLE = "trade_lifecycle"


class ForecastOutcomeObservability(StrEnum):
    """Domain 1 — may the forecast outcome be observed/resolved?

    ``BLOCKED_PENDING_AUDIT_EVIDENCE`` is *not* a discard: the outcome record is
    retained, but price/Brier derivation is withheld until audit-grade provider
    evidence lands.
    """

    OBSERVABLE = "observable"
    BLOCKED_PENDING_AUDIT_EVIDENCE = "blocked_pending_audit_evidence"
    UNIDENTIFIABLE = "unidentifiable"


class CalibrationEligibility(StrEnum):
    """Domain 2 — does the forecast enter the calibration primary cohort?"""

    INCLUDE = "calibration_include"
    EXCLUDE = "calibration_exclude"
    UNIDENTIFIABLE = "calibration_unidentifiable"


class TradePerformanceEligibility(StrEnum):
    """Domain 3 — does the lifecycle enter trade-performance / PnL aggregates?"""

    INCLUDE = "trade_performance_include"
    EXCLUDE = "trade_performance_exclude"
    UNIDENTIFIABLE = "trade_performance_unidentifiable"


class OperationalReliabilityEligibility(StrEnum):
    """Domain 4 — does the lifecycle count towards operational reliability?"""

    INCLUDE = "operational_include"
    EXCLUDE = "operational_exclude"
    UNIDENTIFIABLE = "operational_unidentifiable"


#: The four domain enums, in declaration order.  Used by the guard tests that
#: prove the member sets stay disjoint (no accidental cross-domain assignment).
ELIGIBILITY_DOMAIN_ENUMS: tuple[type[StrEnum], ...] = (
    ForecastOutcomeObservability,
    CalibrationEligibility,
    TradePerformanceEligibility,
    OperationalReliabilityEligibility,
)


@dataclass(frozen=True, slots=True)
class EligibilitySubject:
    """Identity a decision is attached to."""

    kind: EligibilitySubjectKind
    ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EligibilitySubjectKind):
            raise TypeError("kind must be an EligibilitySubjectKind")
        cleaned = self.ref.strip()
        if not cleaned:
            raise EligibilityContractError("empty_subject_ref", "ref must be non-empty")
        object.__setattr__(self, "ref", cleaned)


def canonical_evidence_hash(evidence: Any) -> str:
    """SHA-256 over the canonical JSON encoding of ``evidence``."""

    blob = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """One revision of the four-domain decision for one subject.

    There is intentionally no ``is_valid`` / ``eligible`` / ``success``
    aggregate property.  Collapsing the four domains into one bit is the exact
    failure this contract exists to prevent, and a static guard test asserts no
    such member appears.
    """

    subject: EligibilitySubject
    contract_version: str
    revision_no: int
    supersedes_revision_no: int | None
    forecast_outcome_observability: ForecastOutcomeObservability
    calibration_eligibility: CalibrationEligibility
    trade_performance_eligibility: TradePerformanceEligibility
    operational_reliability_eligibility: OperationalReliabilityEligibility
    decision_reason: str
    evidence_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, EligibilitySubject):
            raise TypeError("subject must be an EligibilitySubject")
        if not isinstance(
            self.forecast_outcome_observability, ForecastOutcomeObservability
        ):
            raise TypeError(
                "forecast_outcome_observability must be a ForecastOutcomeObservability"
            )
        if not isinstance(self.calibration_eligibility, CalibrationEligibility):
            raise TypeError("calibration_eligibility must be a CalibrationEligibility")
        if not isinstance(
            self.trade_performance_eligibility, TradePerformanceEligibility
        ):
            raise TypeError(
                "trade_performance_eligibility must be a TradePerformanceEligibility"
            )
        if not isinstance(
            self.operational_reliability_eligibility, OperationalReliabilityEligibility
        ):
            raise TypeError(
                "operational_reliability_eligibility must be an "
                "OperationalReliabilityEligibility"
            )
        if not self.contract_version.strip():
            raise EligibilityContractError(
                "missing_contract_version", "contract_version must be non-empty"
            )
        if not self.decision_reason.strip():
            raise EligibilityContractError(
                "missing_decision_reason", "decision_reason must be non-empty"
            )
        if not isinstance(self.revision_no, int) or isinstance(self.revision_no, bool):
            raise TypeError("revision_no must be an int")
        if self.revision_no < 1:
            raise EligibilityContractError(
                "invalid_revision_no", "revision_no must be >= 1"
            )
        expected_supersedes = None if self.revision_no == 1 else self.revision_no - 1
        if self.supersedes_revision_no != expected_supersedes:
            raise EligibilityContractError(
                "invalid_supersedes_revision_no",
                (
                    f"revision {self.revision_no} must supersede "
                    f"{expected_supersedes!r}, got {self.supersedes_revision_no!r}"
                ),
            )
        if len(self.evidence_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.evidence_hash
        ):
            raise EligibilityContractError(
                "invalid_evidence_hash", "evidence_hash must be a lowercase sha256 hex"
            )


def unidentifiable_decision(
    subject: EligibilitySubject,
    *,
    contract_version: str = CONTRACT_VERSION,
    reason: str = "no_recorded_eligibility_decision",
) -> EligibilityDecision:
    """The fail-closed default for a subject with no decision on record.

    A missing decision is ``UNIDENTIFIABLE`` in every domain.  It is never
    coalesced to ``INCLUDE`` and never triggers a historical backfill.
    """

    return EligibilityDecision(
        subject=subject,
        contract_version=contract_version,
        revision_no=1,
        supersedes_revision_no=None,
        forecast_outcome_observability=ForecastOutcomeObservability.UNIDENTIFIABLE,
        calibration_eligibility=CalibrationEligibility.UNIDENTIFIABLE,
        trade_performance_eligibility=TradePerformanceEligibility.UNIDENTIFIABLE,
        operational_reliability_eligibility=(
            OperationalReliabilityEligibility.UNIDENTIFIABLE
        ),
        decision_reason=reason,
        evidence_hash=canonical_evidence_hash({"absent": True, "reason": reason}),
    )


def validate_revision_chain(decisions: Sequence[EligibilityDecision]) -> None:
    """Assert ``decisions`` form one gapless, unbranched, acyclic chain.

    Raises :class:`EligibilityContractError` on a duplicate revision (branch), a
    missing revision (gap), a self/backwards reference (cycle), a subject
    mismatch, or a contract-version switch mid-chain.
    """

    if not decisions:
        return
    subject = decisions[0].subject
    version = decisions[0].contract_version
    seen: set[int] = set()
    for decision in decisions:
        if decision.subject != subject:
            raise EligibilityContractError(
                "subject_mismatch", "a revision chain must describe one subject"
            )
        if decision.contract_version != version:
            raise EligibilityContractError(
                "contract_version_switch",
                "a revision chain must stay on one contract version",
            )
        if decision.revision_no in seen:
            raise EligibilityContractError(
                "branched_revision_chain",
                f"revision {decision.revision_no} appears more than once",
            )
        if (
            decision.supersedes_revision_no is not None
            and decision.supersedes_revision_no >= decision.revision_no
        ):
            raise EligibilityContractError(
                "cyclic_revision_chain",
                f"revision {decision.revision_no} cannot supersede "
                f"{decision.supersedes_revision_no}",
            )
        seen.add(decision.revision_no)
    ordered = sorted(seen)
    if ordered != list(range(1, len(ordered) + 1)):
        raise EligibilityContractError(
            "revision_chain_gap", f"revision chain is not contiguous: {ordered}"
        )


def latest_decision(
    decisions: Sequence[EligibilityDecision],
    subject: EligibilitySubject,
    *,
    contract_version: str = CONTRACT_VERSION,
) -> EligibilityDecision:
    """Return the highest revision, or the fail-closed unidentifiable default."""

    if not decisions:
        return unidentifiable_decision(subject, contract_version=contract_version)
    validate_revision_chain(decisions)
    return max(decisions, key=lambda decision: decision.revision_no)


__all__ = [
    "CONTRACT_VERSION",
    "ELIGIBILITY_DOMAIN_ENUMS",
    "KNOWN_CONTRACT_VERSIONS",
    "CalibrationEligibility",
    "EligibilityContractError",
    "EligibilityDecision",
    "EligibilitySubject",
    "EligibilitySubjectKind",
    "ForecastOutcomeObservability",
    "OperationalReliabilityEligibility",
    "TradePerformanceEligibility",
    "canonical_evidence_hash",
    "latest_decision",
    "unidentifiable_decision",
    "validate_revision_chain",
]
