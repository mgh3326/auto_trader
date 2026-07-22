"""Plan-issued identity authority for ROB-974 R3 production evidence.

The public evidence boundaries accept this exact sealed context instead of
caller-provided hashes.  Issuance and every later use independently compare
the supplied plan with a freshly rebuilt canonical production plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rob944_folds import Fold
from rob974_r3_plan import R3ProductionPlan, build_production_r3_plan

__all__ = [
    "R3ProductionEvidenceContext",
    "R3ProductionEvidenceContextError",
    "issue_r3_production_evidence_context",
    "require_r3_production_evidence_context",
]

_ISSUED_CONTEXT_SEAL = object()


class R3ProductionEvidenceContextError(ValueError):
    """Production evidence identity is not issued by the canonical R3 plan."""


@dataclass(frozen=True, slots=True)
class R3ProductionEvidenceContext:
    campaign_identity_sha256: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    ordered_mapping: tuple[tuple[str, str], ...]
    folds: tuple[Fold, ...]
    phases: tuple[str, str]
    operational_status: str
    operational_blocker_reason: str | None
    _plan: R3ProductionPlan = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_issued_context(self)


def _require_seal_and_plan(context: R3ProductionEvidenceContext) -> R3ProductionPlan:
    if context._seal is not _ISSUED_CONTEXT_SEAL:
        raise R3ProductionEvidenceContextError(
            "production evidence context was not code-issued"
        )
    if type(context._plan) is not R3ProductionPlan:
        raise R3ProductionEvidenceContextError(
            "production evidence context requires an exact production plan"
        )
    return context._plan


def _validate_fields_against_plan(
    context: R3ProductionEvidenceContext,
    canonical: R3ProductionPlan,
) -> None:
    _require_seal_and_plan(context)
    if context.campaign_identity_sha256 != canonical.full_campaign_hash:
        raise R3ProductionEvidenceContextError(
            "production evidence context campaign identity drifted"
        )
    if context.campaign_run_id != canonical.campaign_run_id:
        raise R3ProductionEvidenceContextError(
            "production evidence context campaign run ID drifted"
        )
    if context.exact_12_mapping_hash != canonical.exact_12_mapping_hash:
        raise R3ProductionEvidenceContextError(
            "production evidence context mapping hash drifted"
        )
    if context.ordered_mapping != canonical.ordered_mapping:
        raise R3ProductionEvidenceContextError(
            "production evidence context ordered mapping drifted"
        )
    if context.folds != canonical.folds or type(context.folds) is not tuple:
        raise R3ProductionEvidenceContextError(
            "production evidence context folds drifted"
        )
    expected_phases = tuple(phase.upper() for phase in canonical.phases)
    if context.phases != expected_phases or type(context.phases) is not tuple:
        raise R3ProductionEvidenceContextError(
            "production evidence context phases drifted"
        )
    if context.operational_status != canonical.operational_status:
        raise R3ProductionEvidenceContextError(
            "production evidence context operational status drifted"
        )
    if context.operational_blocker_reason != canonical.operational_blocker_reason:
        raise R3ProductionEvidenceContextError(
            "production evidence context blocker reason drifted"
        )


def _validate_issued_context(context: R3ProductionEvidenceContext) -> None:
    plan = _require_seal_and_plan(context)
    canonical = build_production_r3_plan()
    if plan != canonical:
        raise R3ProductionEvidenceContextError(
            "production evidence context plan is not the freshly derived canonical plan"
        )
    _validate_fields_against_plan(context, canonical)


def issue_r3_production_evidence_context(
    plan: object,
) -> R3ProductionEvidenceContext:
    """Issue the sole production-evidence authority from the current plan."""

    if type(plan) is not R3ProductionPlan:
        raise R3ProductionEvidenceContextError(
            "evidence context issuer requires an exact R3ProductionPlan"
        )
    return R3ProductionEvidenceContext(
        campaign_identity_sha256=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        exact_12_mapping_hash=plan.exact_12_mapping_hash,
        ordered_mapping=plan.ordered_mapping,
        folds=plan.folds,
        phases=tuple(phase.upper() for phase in plan.phases),  # type: ignore[arg-type]
        operational_status=plan.operational_status,
        operational_blocker_reason=plan.operational_blocker_reason,
        _plan=plan,
        _seal=_ISSUED_CONTEXT_SEAL,
    )


def require_r3_production_evidence_context(
    context: object,
) -> R3ProductionEvidenceContext:
    """Reject look-alike DTOs, stale plans, and value-mutated issued contexts."""

    if type(context) is not R3ProductionEvidenceContext:
        raise R3ProductionEvidenceContextError(
            "production evidence requires the exact issued context type"
        )
    _validate_issued_context(context)
    return context
