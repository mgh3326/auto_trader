"""ROB-1036 — the only write surface for the eligibility / cleanup tables.

Nothing here calls a broker, a market-data provider, or a scheduler.  The
service appends rows; it never updates or deletes one, and it never resolves a
forecast outcome or derives a price/Brier value.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invalid_sample_eligibility.binding import (
    CLEANUP_PURPOSE,
    CleanupBinding,
    CleanupBindingError,
)
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
    latest_decision,
    validate_revision_chain,
)
from app.services.invalid_sample_eligibility.post_fill import (
    FillEvidenceCompleteness,
    PositionEffectEvidence,
    PostFillCompletion,
    PostFillCompletionStatus,
    evaluate_post_fill_completion,
)
from app.services.invalid_sample_eligibility.repository import (
    InvalidSampleEligibilityRepository,
)

_EVENT_KIND_BY_STATUS = {
    PostFillCompletionStatus.COMPLETE: "post_fill_completion",
    PostFillCompletionStatus.MANUAL_REVIEW: "post_fill_manual_review",
}


def _to_decision(row: Any) -> EligibilityDecision:
    """Rebuild the typed decision from a persisted row, re-validating the hash.

    The stored ``evidence_hash`` is recomputed from the stored ``evidence``. A
    row whose evidence no longer hashes to its digest is refused rather than
    read: a decision is only as trustworthy as the evidence bound to it.
    """

    recomputed = canonical_evidence_hash(
        row.evidence if row.evidence is not None else {}
    )
    if recomputed != row.evidence_hash:
        raise EligibilityContractError(
            "evidence_hash_mismatch",
            (
                f"stored evidence for {row.subject_kind}:{row.subject_ref} "
                f"revision {row.revision_no} does not match its digest"
            ),
        )
    return EligibilityDecision(
        subject=EligibilitySubject(
            kind=EligibilitySubjectKind(row.subject_kind), ref=row.subject_ref
        ),
        contract_version=row.contract_version,
        revision_no=int(row.revision_no),
        supersedes_revision_no=(
            None
            if row.supersedes_revision_no is None
            else int(row.supersedes_revision_no)
        ),
        forecast_outcome_observability=ForecastOutcomeObservability(
            row.forecast_outcome_observability
        ),
        calibration_eligibility=CalibrationEligibility(row.calibration_eligibility),
        trade_performance_eligibility=TradePerformanceEligibility(
            row.trade_performance_eligibility
        ),
        operational_reliability_eligibility=OperationalReliabilityEligibility(
            row.operational_reliability_eligibility
        ),
        decision_reason=row.decision_reason,
        evidence_hash=row.evidence_hash,
    )


class InvalidSampleEligibilityService:
    """Append-only service layer for ``uber-invalid-sample-eligibility.v1``."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = InvalidSampleEligibilityRepository(db)

    # -- eligibility decisions ------------------------------------------------

    async def get_decision(
        self, subject: EligibilitySubject, *, contract_version: str = CONTRACT_VERSION
    ) -> EligibilityDecision:
        """Latest revision, or the fail-closed unidentifiable default.

        A subject with no decision on record is ``UNIDENTIFIABLE`` in all four
        domains.  It is never coalesced to ``INCLUDE``.
        """

        rows = await self._repo.list_decisions(
            subject_kind=subject.kind.value, subject_ref=subject.ref
        )
        decisions = [_to_decision(row) for row in rows]
        return latest_decision(decisions, subject, contract_version=contract_version)

    async def get_decisions(
        self,
        subjects: Sequence[EligibilitySubject],
        *,
        contract_version: str = CONTRACT_VERSION,
    ) -> dict[EligibilitySubject, EligibilityDecision]:
        """Bulk variant of :meth:`get_decision`, same fail-closed default."""

        by_kind: dict[EligibilitySubjectKind, list[str]] = {}
        for subject in subjects:
            by_kind.setdefault(subject.kind, []).append(subject.ref)

        chains: dict[tuple[str, str], list[EligibilityDecision]] = {}
        for kind, refs in by_kind.items():
            rows = await self._repo.list_decisions_for_refs(
                subject_kind=kind.value, subject_refs=refs
            )
            for row in rows:
                chains.setdefault((row.subject_kind, row.subject_ref), []).append(
                    _to_decision(row)
                )

        return {
            subject: latest_decision(
                chains.get((subject.kind.value, subject.ref), []),
                subject,
                contract_version=contract_version,
            )
            for subject in subjects
        }

    async def record_decision(
        self,
        *,
        subject: EligibilitySubject,
        forecast_outcome_observability: ForecastOutcomeObservability,
        calibration_eligibility: CalibrationEligibility,
        trade_performance_eligibility: TradePerformanceEligibility,
        operational_reliability_eligibility: OperationalReliabilityEligibility,
        decision_reason: str,
        decided_by: str,
        evidence: dict[str, Any] | None = None,
        contract_version: str = CONTRACT_VERSION,
    ) -> EligibilityDecision:
        """Append the next revision. A correction supersedes; it never overwrites."""

        existing_rows = await self._repo.list_decisions(
            subject_kind=subject.kind.value, subject_ref=subject.ref
        )
        existing = [_to_decision(row) for row in existing_rows]
        validate_revision_chain(existing)
        if existing and existing[-1].contract_version != contract_version:
            raise EligibilityContractError(
                "contract_version_switch",
                (
                    f"subject is recorded under {existing[-1].contract_version!r}; "
                    f"refusing to append {contract_version!r}"
                ),
            )
        next_revision = len(existing) + 1
        evidence_payload = dict(evidence or {})
        decision = EligibilityDecision(
            subject=subject,
            contract_version=contract_version,
            revision_no=next_revision,
            supersedes_revision_no=None if next_revision == 1 else next_revision - 1,
            forecast_outcome_observability=forecast_outcome_observability,
            calibration_eligibility=calibration_eligibility,
            trade_performance_eligibility=trade_performance_eligibility,
            operational_reliability_eligibility=operational_reliability_eligibility,
            decision_reason=decision_reason,
            evidence_hash=canonical_evidence_hash(evidence_payload),
        )
        await self._repo.add_decision(
            {
                "subject_kind": subject.kind.value,
                "subject_ref": subject.ref,
                "contract_version": decision.contract_version,
                "revision_no": decision.revision_no,
                "supersedes_revision_no": decision.supersedes_revision_no,
                "forecast_outcome_observability": (
                    decision.forecast_outcome_observability.value
                ),
                "calibration_eligibility": decision.calibration_eligibility.value,
                "trade_performance_eligibility": (
                    decision.trade_performance_eligibility.value
                ),
                "operational_reliability_eligibility": (
                    decision.operational_reliability_eligibility.value
                ),
                "decision_reason": decision.decision_reason,
                "decided_by": decided_by,
                "evidence": evidence_payload,
                "evidence_hash": decision.evidence_hash,
            }
        )
        return decision

    # -- cleanup binding ------------------------------------------------------

    async def record_cleanup_binding(self, binding: CleanupBinding) -> Any:
        """Persist the immutable binding, or return the identical existing row.

        Re-authoring the same binding is idempotent.  A *different* binding for
        an already-bound ``client_order_id`` is refused: the identity is claimed.
        """

        if binding.purpose != CLEANUP_PURPOSE:
            raise CleanupBindingError(
                "unsupported_purpose", f"purpose must be {CLEANUP_PURPOSE!r}"
            )
        existing = await self._repo.get_binding_by_client_order_id(
            binding.client_order_id
        )
        if existing is not None:
            if existing.binding_hash != binding.binding_hash:
                raise CleanupBindingError(
                    "conflicting_binding_for_client_order_id",
                    (
                        f"client_order_id {binding.client_order_id!r} is already "
                        "bound to a different approval/mission identity"
                    ),
                )
            return existing
        return await self._repo.add_binding(
            {
                "purpose": binding.purpose,
                "contract_version": binding.contract_version,
                "forecast_id": binding.forecast_id,
                "sample_ref": binding.sample_ref,
                "approval_id": binding.approval_id,
                "approval_hash": binding.approval_hash,
                "approval_expires_at": binding.approval_expires_at,
                "approval_session_id": binding.approval_session_id,
                "mission_id": binding.mission_id,
                "account_mode": binding.account_mode,
                "client_order_id": binding.client_order_id,
                "lifecycle_correlation_id": binding.lifecycle_correlation_id,
                "binding_hash": binding.binding_hash,
            }
        )

    # -- post-fill evidence ---------------------------------------------------

    async def record_post_fill_evidence(
        self,
        *,
        binding: CleanupBinding,
        fill_evidence: FillEvidenceCompleteness,
        position_effect: PositionEffectEvidence,
        evidence: dict[str, Any] | None = None,
    ) -> PostFillCompletion:
        """Evaluate the two-evidence gate and append the resulting event.

        Terminal success requires complete broker fill evidence *and* consistent
        position-effect evidence.  Anything else is recorded as a typed
        manual-review event.  Replaying identical evidence appends nothing.
        """

        stored = await self._repo.get_binding_by_hash(binding.binding_hash)
        if stored is None:
            raise CleanupBindingError(
                "unbound_lifecycle",
                "post-fill evidence requires a persisted cleanup binding",
            )
        completion = evaluate_post_fill_completion(
            fill_evidence=fill_evidence, position_effect=position_effect
        )
        evidence_payload = {
            "fill_evidence": fill_evidence.value,
            "position_effect_evidence": position_effect.value,
            "detail": dict(evidence or {}),
        }
        evidence_hash = canonical_evidence_hash(evidence_payload)
        event_kind = _EVENT_KIND_BY_STATUS[completion.status]
        duplicate = await self._repo.get_lifecycle_event(
            binding_hash=binding.binding_hash,
            event_kind=event_kind,
            evidence_hash=evidence_hash,
        )
        if duplicate is None:
            await self._repo.add_lifecycle_event(
                {
                    "binding_hash": binding.binding_hash,
                    "client_order_id": binding.client_order_id,
                    "contract_version": binding.contract_version,
                    "event_kind": event_kind,
                    "completion_status": completion.status.value,
                    "manual_review_reason": (
                        None if completion.reason is None else completion.reason.value
                    ),
                    "fill_evidence": fill_evidence.value,
                    "position_effect_evidence": position_effect.value,
                    "evidence": evidence_payload,
                    "evidence_hash": evidence_hash,
                }
            )
        return completion


__all__ = ["InvalidSampleEligibilityService"]
