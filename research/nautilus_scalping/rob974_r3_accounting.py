"""Independent ROB-974 R3 exact-12 terminal-attempt accounting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rob974_h6a_accounting import CLOSED_STATUSES, AttemptAccountingRow
from rob974_r3_shape import (
    R3_CANONICAL_ROW_ORDER,
    Exact12MappingError,
    compute_exact_12_mapping_hash,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "Exact12AccountingError",
    "Exact12AccountingReport",
    "build_exact_12_accounting",
]


class Exact12AccountingError(ValueError):
    """R3 accounting evidence is malformed, reordered, retried, or foreign."""


@dataclass(frozen=True, slots=True)
class Exact12AccountingReport:
    campaign_run_id: str
    exact_12_mapping_hash: str
    expected_total: int
    registered_total: int
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    status_counts: Mapping[str, int]
    missing_row_ids: tuple[str, ...]
    extra_experiment_ids: tuple[str, ...]
    mismatch_row_ids: tuple[str, ...]
    duplicate_or_gap_row_ids: tuple[str, ...]
    accounting_complete: bool
    all_primary_completed: bool
    performance_usable: bool
    trial_accounting_hash: str


def build_exact_12_accounting(
    *,
    campaign_run_id: str,
    ordered_mapping: tuple[tuple[str, str], ...],
    registered_total: int,
    attempts: tuple[AttemptAccountingRow, ...],
    mismatch_row_ids: tuple[str, ...] = (),
    extra_experiment_ids: tuple[str, ...] = (),
) -> Exact12AccountingReport:
    """Reconstruct exact-12 coverage without deriving authority from input size.

    Partial primary evidence is represented as structurally incomplete.  A
    retry, duplicate, foreign row, or persisted order drift is malformed and
    raises instead of being normalized into an economic result.
    """

    try:
        mapping_hash = compute_exact_12_mapping_hash(ordered_mapping)
    except Exact12MappingError as exc:
        raise Exact12AccountingError(str(exc)) from exc
    if type(campaign_run_id) is not str or not campaign_run_id:
        raise Exact12AccountingError("campaign_run_id must be non-empty exact str")
    if type(registered_total) is not int or not 0 <= registered_total <= 12:
        raise Exact12AccountingError("registered_total must be an exact int in [0,12]")
    if type(attempts) is not tuple or any(
        type(attempt) is not AttemptAccountingRow for attempt in attempts
    ):
        raise Exact12AccountingError(
            "attempts must be an exact tuple of AttemptAccountingRow values"
        )
    if len(attempts) > 12:
        raise Exact12AccountingError(
            "R3 accounting cannot contain more than 12 attempts"
        )
    if type(mismatch_row_ids) is not tuple or type(extra_experiment_ids) is not tuple:
        raise Exact12AccountingError("mismatch/extra evidence must use exact tuples")

    expected_experiments = dict(ordered_mapping)
    actual_row_ids = tuple(attempt.row_id for attempt in attempts)
    if len(set(actual_row_ids)) != len(actual_row_ids):
        raise Exact12AccountingError("R3 attempts contain duplicate row IDs")
    if not set(actual_row_ids) <= set(R3_CANONICAL_ROW_ORDER):
        raise Exact12AccountingError("R3 attempts contain a foreign row ID")
    canonical_subsequence = tuple(
        row_id for row_id in R3_CANONICAL_ROW_ORDER if row_id in set(actual_row_ids)
    )
    if actual_row_ids != canonical_subsequence:
        raise Exact12AccountingError("R3 persisted/recomputed attempt order drift")
    for attempt in attempts:
        if attempt.retry_index != 0:
            raise Exact12AccountingError("R3 exact-12 primary surface forbids retries")
        if attempt.experiment_id != expected_experiments[attempt.row_id]:
            raise Exact12AccountingError(
                f"{attempt.row_id}: attempt experiment ID differs from sealed mapping"
            )

    if len(set(mismatch_row_ids)) != len(mismatch_row_ids) or not set(
        mismatch_row_ids
    ) <= set(R3_CANONICAL_ROW_ORDER):
        raise Exact12AccountingError("mismatch_row_ids are duplicated or foreign")
    canonical_experiment_ids = frozenset(expected_experiments.values())
    if (
        len(set(extra_experiment_ids)) != len(extra_experiment_ids)
        or set(extra_experiment_ids) & canonical_experiment_ids
    ):
        raise Exact12AccountingError(
            "extra_experiment_ids are duplicated or overlap the exact-12 mapping"
        )
    if set(mismatch_row_ids) & set(actual_row_ids):
        raise Exact12AccountingError(
            "a mismatched registration cannot also carry its expected primary attempt"
        )

    missing = tuple(
        row_id
        for row_id in R3_CANONICAL_ROW_ORDER
        if row_id not in set(actual_row_ids) and row_id not in set(mismatch_row_ids)
    )
    status_counts = dict.fromkeys(CLOSED_STATUSES, 0)
    for attempt in attempts:
        status_counts[attempt.status] += 1
    primary_attempts = len(attempts)
    accounting_complete = (
        registered_total == 12
        and primary_attempts == 12
        and not missing
        and not mismatch_row_ids
        and not extra_experiment_ids
    )
    all_primary_completed = accounting_complete and all(
        attempt.status == "completed" for attempt in attempts
    )
    performance_usable = all_primary_completed

    hash_payload = {
        "campaign_run_id": campaign_run_id,
        "exact_12_mapping_hash": mapping_hash,
        "expected_total": 12,
        "registered_total": registered_total,
        "primary_attempts": primary_attempts,
        "total_attempts": primary_attempts,
        "retry_attempts": 0,
        "status_counts": status_counts,
        "missing_row_ids": list(missing),
        "extra_experiment_ids": sorted(extra_experiment_ids),
        "mismatch_row_ids": sorted(mismatch_row_ids),
        "duplicate_or_gap_row_ids": [],
        "attempts": [
            {
                "row_id": attempt.row_id,
                "experiment_id": attempt.experiment_id,
                "retry_index": attempt.retry_index,
                "status": attempt.status,
                "reason_code": attempt.reason_code,
                "fold_evidence_hash": attempt.fold_evidence_hash,
                "run_identity": attempt.run_identity,
            }
            for attempt in attempts
        ],
    }
    return Exact12AccountingReport(
        campaign_run_id=campaign_run_id,
        exact_12_mapping_hash=mapping_hash,
        expected_total=12,
        registered_total=registered_total,
        primary_attempts=primary_attempts,
        total_attempts=primary_attempts,
        retry_attempts=0,
        status_counts=status_counts,
        missing_row_ids=missing,
        extra_experiment_ids=tuple(sorted(extra_experiment_ids)),
        mismatch_row_ids=tuple(sorted(mismatch_row_ids)),
        duplicate_or_gap_row_ids=(),
        accounting_complete=accounting_complete,
        all_primary_completed=all_primary_completed,
        performance_usable=performance_usable,
        trial_accounting_hash=canonical_sha256(hash_payload),
    )
