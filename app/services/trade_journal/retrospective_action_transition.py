from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import TradeRetrospective, TradeRetrospectiveAction
from app.services.trade_journal.retrospective_action_repository import (
    ActionControlError,
    RetrospectiveActionRepository,
)
from app.services.trade_journal.retrospective_action_types import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    REASON_MAX_LENGTH,
    TERMINAL_STATUSES,
    ActionControlModeError,
    ActionNotFoundError,
    ActionStatus,
    ActionTransitionConflict,
    ActionTransitionInvalid,
    ActionTransitionResult,
    TransitionActor,
    validate_operator_attestation,
)


def _normalize_reason(reason: str | None, *, required: bool) -> str | None:
    if reason is None:
        if required:
            raise ActionTransitionInvalid("a non-blank reason is required")
        return None
    if not isinstance(reason, str):
        raise ActionTransitionInvalid("reason must be a string")
    normalized = reason.strip()
    if not normalized:
        if required:
            raise ActionTransitionInvalid("a non-blank reason is required")
        return None
    if len(normalized) > REASON_MAX_LENGTH:
        raise ActionTransitionInvalid(f"reason exceeds {REASON_MAX_LENGTH} characters")
    return normalized


def _validate_transition_payload(
    *,
    target_status: str,
    actor: TransitionActor,
    reason: str | None,
    evidence: dict[str, object] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    normalized_reason = _normalize_reason(
        reason, required=target_status in {"obsolete", "expired"}
    )

    if target_status in ACTIVE_STATUSES:
        if evidence is not None:
            raise ActionTransitionInvalid(
                "evidence is only permitted for terminal transitions"
            )
        return normalized_reason, None

    if target_status == "expired" or (
        target_status == "done" and actor.source == "reconciler"
    ):
        if evidence is None:
            raise ActionTransitionInvalid(
                f"{target_status} by {actor.source} requires authoritative evidence"
            )
        return normalized_reason, validate_operator_attestation(evidence)

    if evidence is None:
        return normalized_reason, None
    return normalized_reason, validate_operator_attestation(evidence)


def _snapshot(
    action: TradeRetrospectiveAction,
    *,
    status: str | None = None,
    version: int | None = None,
    changed_at: datetime | None = None,
    resolved_at: datetime | None = None,
    actor: TransitionActor | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    effective_status = status if status is not None else action.status
    return {
        "action_id": str(action.id),
        "status": effective_status,
        "version": version if version is not None else action.version,
        "status_changed_at": (
            changed_at if changed_at is not None else action.status_changed_at
        ).isoformat(),
        "resolved_at": (
            resolved_at if resolved_at is not None else action.resolved_at
        ).isoformat()
        if (resolved_at is not None or action.resolved_at is not None)
        else None,
        "status_actor": actor.value if actor is not None else action.status_actor,
        "status_source": actor.source if actor is not None else action.status_source,
        "status_reason": reason if status is not None else action.status_reason,
    }


def _conflict(
    action: TradeRetrospectiveAction, reason: str
) -> ActionTransitionConflict:
    return ActionTransitionConflict(
        action_id=action.id,
        status=action.status,
        version=action.version,
        reason=reason,
    )


def _terminal_retry_matches(
    action: TradeRetrospectiveAction,
    *,
    reason: str | None,
    evidence: dict[str, object] | None,
) -> bool:
    try:
        normalized_reason = _normalize_reason(
            reason, required=action.status in {"obsolete", "expired"}
        )
        normalized_evidence = (
            validate_operator_attestation(evidence) if evidence is not None else None
        )
    except ActionTransitionInvalid:
        return False
    return (
        normalized_reason == action.status_reason
        and normalized_evidence == action.status_evidence
    )


async def transition_retrospective_action(
    db: AsyncSession,
    *,
    action_id: UUID,
    target_status: ActionStatus,
    expected_version: int,
    actor: TransitionActor,
    reason: str | None,
    evidence: dict[str, object] | None,
    dry_run: bool = False,
) -> ActionTransitionResult:
    """Evaluate and optionally persist one canonical action transition."""
    if target_status not in ALL_STATUSES:
        raise ActionTransitionInvalid(f"unknown target status: {target_status!r}")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise ActionTransitionInvalid("expected_version must be an integer")
    if expected_version < 1:
        raise ActionTransitionInvalid("expected_version must be at least 1")
    if len(actor.value) > 128:
        raise ActionTransitionInvalid("transition actor exceeds 128 characters")

    parent_id = await db.scalar(
        select(TradeRetrospectiveAction.retrospective_id).where(
            TradeRetrospectiveAction.id == action_id
        )
    )
    if parent_id is None:
        raise ActionNotFoundError(action_id)

    repository = RetrospectiveActionRepository(db)
    try:
        mode = await repository.get_control_mode()
    except ActionControlError as exc:
        raise ActionControlModeError(None) from exc
    if mode != "canonical":
        raise ActionControlModeError(mode)

    parent = await db.scalar(
        select(TradeRetrospective)
        .where(TradeRetrospective.id == parent_id)
        .with_for_update()
    )
    if parent is None:
        raise ActionNotFoundError(action_id)

    locked_children = list(
        (
            await db.scalars(
                select(TradeRetrospectiveAction)
                .where(TradeRetrospectiveAction.retrospective_id == parent_id)
                .order_by(TradeRetrospectiveAction.id)
                .with_for_update()
            )
        ).all()
    )
    action = next((child for child in locked_children if child.id == action_id), None)
    if action is None:
        raise ActionNotFoundError(action_id)

    if action.status in TERMINAL_STATUSES:
        if target_status != action.status:
            raise _conflict(action, "terminal actions cannot transition or reopen")
        if not _terminal_retry_matches(action, reason=reason, evidence=evidence):
            raise _conflict(action, "terminal audit payload does not match")
        return ActionTransitionResult(
            changed=False,
            idempotent=True,
            dry_run=dry_run,
            action=_snapshot(action),
        )

    if expected_version != action.version:
        raise _conflict(action, "expected_version does not match current version")

    if target_status == action.status:
        _validate_transition_payload(
            target_status=target_status,
            actor=actor,
            reason=reason,
            evidence=evidence,
        )
        return ActionTransitionResult(
            changed=False,
            idempotent=True,
            dry_run=dry_run,
            action=_snapshot(action),
        )

    normalized_reason, normalized_evidence = _validate_transition_payload(
        target_status=target_status,
        actor=actor,
        reason=reason,
        evidence=evidence,
    )
    changed_at = now_kst()
    resolved_at = changed_at if target_status in TERMINAL_STATUSES else None
    next_version = action.version + 1

    if dry_run:
        return ActionTransitionResult(
            changed=True,
            idempotent=False,
            dry_run=True,
            action=_snapshot(
                action,
                status=target_status,
                version=next_version,
                changed_at=changed_at,
                resolved_at=resolved_at,
                actor=actor,
                reason=normalized_reason,
            ),
        )

    action.status = target_status
    action.version = next_version
    action.status_changed_at = changed_at
    action.status_actor = actor.value
    action.status_source = actor.source
    action.status_reason = normalized_reason
    action.status_evidence = normalized_evidence
    action.resolved_at = resolved_at
    await db.flush()
    await repository.rebuild_projection(parent_id)

    return ActionTransitionResult(
        changed=True,
        idempotent=False,
        dry_run=False,
        action=_snapshot(action),
    )
