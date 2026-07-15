"""Evaluation epoch management for ROB-850.

A new epoch is created on:
- broker account reset
- API-key recreation
- frozen initial-equity change

Prior epochs remain separately queryable and are NEVER spliced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EpochResetReason,
    EvaluationConfigError,
    ViewName,
)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _equity_differs(
    current: dict[ViewName, Decimal],
    new: dict[ViewName, Decimal],
) -> bool:
    if set(current) != set(new):
        return True
    return any(current[view] != new[view] for view in current)


def should_create_new_epoch(
    current: EpochIdentity,
    *,
    reset_reason: EpochResetReason,
    new_initial_equity: dict[ViewName, Decimal] | None = None,
) -> bool:
    """Return True if a new evaluation epoch must be created.

    * ``account_reset`` — always True
    * ``api_key_recreation`` — always True
    * ``initial_equity_change`` — True iff ``new_initial_equity`` differs
      from ``current.initial_equity``. When ``new_initial_equity`` is
      ``None`` the trigger is honoured conservatively (treated as a change).
    """
    if reset_reason is EpochResetReason.ACCOUNT_RESET:
        return True
    if reset_reason is EpochResetReason.API_KEY_RECREATION:
        return True
    if reset_reason is EpochResetReason.INITIAL_EQUITY_CHANGE:
        if new_initial_equity is None:
            return True
        return _equity_differs(current.initial_equity, new_initial_equity)
    return False


def create_epoch_identity(
    *,
    epoch_id: str,
    assignment_id: str,
    validation_id: str,
    cohort_id: str,
    config_hash: str,
    experiment_hash: str,
    cohort_hash: str,
    initial_equity: dict[ViewName, Decimal],
    started_at: datetime,
    reset_reason: EpochResetReason | None = None,
    prior_epoch_id: str | None = None,
) -> EpochIdentity:
    """Validate and return an :class:`EpochIdentity`.

    The frozen contract enforces that ``started_at`` is timezone-aware and
    that ``initial_equity`` covers exactly the three V1 views.
    """
    return EpochIdentity(
        epoch_id=epoch_id,
        assignment_id=assignment_id,
        validation_id=validation_id,
        cohort_id=cohort_id,
        config_hash=config_hash,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        initial_equity=initial_equity,
        started_at=started_at,
        reset_reason=reset_reason,
        prior_epoch_id=prior_epoch_id,
    )


def compute_calendar_days(start: datetime, end: datetime) -> int:
    """Full calendar days between two timezone-aware datetimes.

    A full day is an actually elapsed 24-hour period after normalizing both
    endpoints to UTC. Local date boundaries and mixed offsets cannot advance
    eligibility early.

    Examples:
        2026-01-01T12:00 → 2026-01-02T12:00  = 1 full calendar day
        2026-01-01T12:00 → 2026-01-08T12:00  = 7 full calendar days
        2026-01-01T12:00 → 2026-01-07T12:00  = 6 full calendar days
    """
    if not _is_aware(start):
        raise EvaluationConfigError("invalid_epoch", "start must be timezone-aware")
    if not _is_aware(end):
        raise EvaluationConfigError("invalid_epoch", "end must be timezone-aware")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if end_utc < start_utc:
        raise EvaluationConfigError(
            "invalid_evaluation_window", "evaluation end precedes start"
        )
    return (end_utc - start_utc).days


def is_epoch_active(epoch: EpochIdentity, *, now: datetime) -> bool:
    """Return True if ``epoch`` has started at or before ``now``.

    Successorship is resolved externally via :func:`filter_epochs_by_cohort`
    over the full epoch list. This pure check confirms the epoch's clock has
    begun relative to ``now``.
    """
    return epoch.started_at <= now


def filter_epochs_by_cohort(
    epochs: list[EpochIdentity], cohort_id: str
) -> list[EpochIdentity]:
    """Return all epochs for ``cohort_id`` sorted by ``started_at`` ascending.

    Prior epochs remain separately queryable and are never spliced.
    """
    return sorted(
        (epoch for epoch in epochs if epoch.cohort_id == cohort_id),
        key=lambda epoch: epoch.started_at,
    )


__all__ = [
    "compute_calendar_days",
    "create_epoch_identity",
    "filter_epochs_by_cohort",
    "is_epoch_active",
    "should_create_new_epoch",
]
