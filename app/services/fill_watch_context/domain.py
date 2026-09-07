"""Closed, non-economic outcome vocabulary and deterministic projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.services.fill_watch_context.contracts import ContextArtifact

__all__ = [
    "CONTEXTUAL_STATUS_VALUES",
    "ContextReason",
    "ContextualStatus",
    "NextAction",
    "OutcomeProjection",
    "project_context_outcome",
]


class ContextualStatus(StrEnum):
    """Every business result allowed in the Phase 0 outcome table."""

    CONTEXT_ONLY_NO_ACTION = "context_only_no_action"
    STALE_INPUT = "stale_input"
    FAILED_PROCESSING = "failed_processing"
    NEEDS_HUMAN = "needs_human"


class ContextReason(StrEnum):
    CONTEXT_RECORDED = "context_recorded"
    CLOSE_CONDITION_RECORDED = "close_condition_recorded"
    STALE_MARKET_SNAPSHOT = "stale_market_snapshot"
    STALE_POSITION_SNAPSHOT = "stale_position_snapshot"
    ARTIFACT_DECLARED_FAILED = "artifact_declared_failed"
    ROUTE_UNAVAILABLE = "route_unavailable"
    EVENT_UUID_CONFLICT = "event_uuid_conflict"


class NextAction(StrEnum):
    NONE = "none"
    REFRESH_CONTEXT = "refresh_context"
    INSPECT_ARTIFACT = "inspect_artifact"
    AWAIT_ROUTE = "await_route"
    OPERATOR_REVIEW = "operator_review"


CONTEXTUAL_STATUS_VALUES = tuple(member.value for member in ContextualStatus)


@dataclass(frozen=True)
class OutcomeProjection:
    """A context-only classification derived from supplied data, not a fetch."""

    contextual_status: ContextualStatus
    reason: ContextReason
    next_action: NextAction


def project_context_outcome(artifact: ContextArtifact) -> OutcomeProjection:
    """Project the only non-economic outcome allowed by this phase.

    Ordering is intentional: an incomplete artifact wins over a supplied route;
    an unavailable route wins over otherwise fresh input; stale snapshots are
    visible before a benign close-condition observation. No branch can create
    an economic intent.
    """
    context = artifact.context
    if context.artifact_health == "failed":
        return OutcomeProjection(
            ContextualStatus.FAILED_PROCESSING,
            ContextReason.ARTIFACT_DECLARED_FAILED,
            NextAction.INSPECT_ARTIFACT,
        )
    if context.route.availability == "unavailable":
        return OutcomeProjection(
            ContextualStatus.NEEDS_HUMAN,
            ContextReason.ROUTE_UNAVAILABLE,
            NextAction.AWAIT_ROUTE,
        )
    if context.market_snapshot.freshness == "stale":
        return OutcomeProjection(
            ContextualStatus.STALE_INPUT,
            ContextReason.STALE_MARKET_SNAPSHOT,
            NextAction.REFRESH_CONTEXT,
        )
    if context.position_snapshot.freshness == "stale":
        return OutcomeProjection(
            ContextualStatus.STALE_INPUT,
            ContextReason.STALE_POSITION_SNAPSHOT,
            NextAction.REFRESH_CONTEXT,
        )
    if (
        context.event_kind == "watch"
        and context.watch
        and context.watch.close_condition
    ):
        return OutcomeProjection(
            ContextualStatus.CONTEXT_ONLY_NO_ACTION,
            ContextReason.CLOSE_CONDITION_RECORDED,
            NextAction.NONE,
        )
    return OutcomeProjection(
        ContextualStatus.CONTEXT_ONLY_NO_ACTION,
        ContextReason.CONTEXT_RECORDED,
        NextAction.NONE,
    )
