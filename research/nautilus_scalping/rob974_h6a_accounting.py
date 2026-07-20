"""ROB-981 (ROB-974 R2 H6-A) CP4 -- independent exact-48 accounting, retry
semantics, and trial seal.

Builds ONE combined report that independently reconstructs expected/
registered/primary/total/retry/status/missing/extra/mismatch/duplicate-gap
fields across all 48 canonical row IDs in canonical order -- NEVER two
self-attested 24-row reports summed together, and never a count-only proof
(every attempt in the supplied evidence is walked individually).

``accounting_complete`` means structurally valid terminal-evidence coverage,
NOT performance PASS -- all 48 rejected/crashed/timeout primaries can still
be "complete" (a real, complete record of a failed run) while
``performance_usable=False``. ``performance_usable`` additionally requires
every primary ``status=="completed"`` and zero retries anywhere.

``mismatch_row_ids``/``extra_experiment_ids`` are REGISTRATION-time facts
this module cannot independently observe from terminal attempt evidence
alone (mirrors ``rob945_accounting_seal``'s documented trust boundary) --
they are caller-supplied assertions, validated for shape/domain-membership
where knowable and folded into the tamper-evident ``trial_accounting_hash``,
never silently re-derived from nothing.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
shared ``research_contracts.canonical_hash`` authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "CLOSED_STATUSES",
    "EXPECTED_TOTAL_ROWS",
    "AccountingInputError",
    "AttemptAccountingRow",
    "CombinedAccountingReport",
    "build_combined_accounting",
]

EXPECTED_TOTAL_ROWS = 48
CLOSED_STATUSES: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")


class AccountingInputError(ValueError):
    """Malformed or out-of-plan accounting input -- refused before any
    report is built (a cross-campaign/out-of-plan row_id, a non-canonical
    experiment_id set, or a type-drifted status field)."""


@dataclass(frozen=True)
class AttemptAccountingRow:
    """One recorded attempt's accounting-relevant facts -- coarser than the
    full CP3 ``AttemptRecord`` (accounting only needs identity + status)."""

    row_id: str
    experiment_id: str
    retry_index: int
    status: str

    def __post_init__(self) -> None:
        if type(self.row_id) is not str:
            raise AccountingInputError("row_id must be str")
        if type(self.experiment_id) is not str:
            raise AccountingInputError("experiment_id must be str")
        if type(self.retry_index) is not int or self.retry_index < 0:
            raise AccountingInputError(
                "retry_index must be a non-negative built-in int"
            )
        if self.status not in CLOSED_STATUSES:
            raise AccountingInputError(f"status must be one of {CLOSED_STATUSES}")


@dataclass(frozen=True)
class CombinedAccountingReport:
    campaign_run_id: str
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


def _is_contiguous_from_zero(sorted_unique_indices: list[int]) -> bool:
    return sorted_unique_indices == list(range(len(sorted_unique_indices)))


def _is_clean_group(rows: list[AttemptAccountingRow]) -> bool:
    indices = [r.retry_index for r in rows]
    unique_sorted = sorted(set(indices))
    return len(indices) == len(unique_sorted) and _is_contiguous_from_zero(
        unique_sorted
    )


def _is_duplicate_or_gap(rows: list[AttemptAccountingRow]) -> bool:
    indices = [r.retry_index for r in rows]
    unique_sorted = sorted(set(indices))
    if len(indices) != len(unique_sorted):
        return True
    if 0 not in unique_sorted:
        return False
    return not _is_contiguous_from_zero(unique_sorted)


def build_combined_accounting(
    *,
    campaign_run_id: str,
    canonical_row_ids: tuple[str, ...],
    row_id_to_experiment_id: Mapping[str, str],
    registered_total: int,
    attempts: Sequence[AttemptAccountingRow],
    mismatch_row_ids: Sequence[str] = (),
    extra_experiment_ids: Sequence[str] = (),
) -> CombinedAccountingReport:
    """Independently reconstruct the combined 48-row accounting report.

    Fail-closed (raises ``AccountingInputError``) BEFORE building a report
    for: a non-exact-48 ``canonical_row_ids``, a row_id/experiment_id pair
    outside that canonical mapping (cross-campaign/out-of-plan evidence),
    type-drifted fields, or a caller-asserted mismatch/extra id outside its
    required domain.
    """
    if len(canonical_row_ids) != EXPECTED_TOTAL_ROWS or len(
        set(canonical_row_ids)
    ) != len(canonical_row_ids):
        raise AccountingInputError(
            f"canonical_row_ids must be exactly {EXPECTED_TOTAL_ROWS} unique row IDs"
        )
    if set(row_id_to_experiment_id) != set(canonical_row_ids):
        raise AccountingInputError(
            "row_id_to_experiment_id must cover exactly the canonical_row_ids set"
        )
    if type(registered_total) is not int or registered_total < 0:
        raise AccountingInputError(
            "registered_total must be a non-negative built-in int"
        )

    canonical_experiment_ids = frozenset(row_id_to_experiment_id.values())
    mismatch_row_ids = tuple(mismatch_row_ids)
    extra_experiment_ids = tuple(extra_experiment_ids)
    if len(set(mismatch_row_ids)) != len(mismatch_row_ids):
        raise AccountingInputError("mismatch_row_ids must not contain duplicates")
    if not set(mismatch_row_ids) <= set(canonical_row_ids):
        raise AccountingInputError(
            "mismatch_row_ids must be a subset of canonical_row_ids"
        )
    if len(set(extra_experiment_ids)) != len(extra_experiment_ids):
        raise AccountingInputError("extra_experiment_ids must not contain duplicates")
    if set(extra_experiment_ids) & canonical_experiment_ids:
        raise AccountingInputError(
            "extra_experiment_ids must NOT overlap the canonical experiment_id set (by "
            "definition an 'extra' registration is outside the expected 48)"
        )

    by_row: dict[str, list[AttemptAccountingRow]] = {
        row_id: [] for row_id in canonical_row_ids
    }
    for attempt in attempts:
        if attempt.row_id not in by_row:
            raise AccountingInputError(
                f"attempt row_id is not one of the canonical {EXPECTED_TOTAL_ROWS} rows -- "
                "cross-campaign/out-of-plan evidence refused"
            )
        expected_experiment_id = row_id_to_experiment_id[attempt.row_id]
        if attempt.experiment_id != expected_experiment_id:
            raise AccountingInputError(
                f"attempt for row_id {attempt.row_id!r} carries an experiment_id that does "
                "not match the trusted expected mapping"
            )
        by_row[attempt.row_id].append(attempt)

    mismatch_set = frozenset(mismatch_row_ids)
    # A row already asserted as a registration-time mismatch cannot ALSO
    # carry terminal attempt evidence under its own expected experiment_id
    # (that would mean evidence exists for a registration this report itself
    # says never happened as expected).
    for row_id in mismatch_set:
        if by_row[row_id]:
            raise AccountingInputError(
                f"row_id {row_id!r} is asserted as mismatch but also carries terminal "
                "attempt evidence under its own expected experiment_id -- contradiction"
            )

    missing = sorted(
        row_id
        for row_id in canonical_row_ids
        if row_id not in mismatch_set
        and 0 not in {r.retry_index for r in by_row[row_id]}
    )
    duplicate_or_gap = sorted(
        row_id for row_id, rows in by_row.items() if rows and _is_duplicate_or_gap(rows)
    )

    clean_rows: list[AttemptAccountingRow] = []
    for rows in by_row.values():
        if rows and _is_clean_group(rows):
            clean_rows.extend(rows)

    total_attempts = len(clean_rows)
    primary_attempts = sum(
        1 for rows in by_row.values() if rows and _is_clean_group(rows)
    )
    retry_attempts = total_attempts - primary_attempts
    status_counts = dict.fromkeys(CLOSED_STATUSES, 0)
    for row in clean_rows:
        status_counts[row.status] += 1

    accounting_complete = not (
        missing or extra_experiment_ids or mismatch_row_ids or duplicate_or_gap
    )
    all_primary_completed = accounting_complete and all(
        any(r.retry_index == 0 and r.status == "completed" for r in by_row[row_id])
        for row_id in canonical_row_ids
    )
    performance_usable = (
        accounting_complete and all_primary_completed and retry_attempts == 0
    )

    normalized_attempts = sorted(
        (a for rows in by_row.values() for a in rows),
        key=lambda a: (canonical_row_ids.index(a.row_id), a.retry_index),
    )
    report_for_hash = {
        "campaign_run_id": campaign_run_id,
        "expected_total": EXPECTED_TOTAL_ROWS,
        "registered_total": registered_total,
        "primary_attempts": primary_attempts,
        "total_attempts": total_attempts,
        "retry_attempts": retry_attempts,
        "status_counts": status_counts,
        "missing_row_ids": missing,
        "extra_experiment_ids": sorted(extra_experiment_ids),
        "mismatch_row_ids": sorted(mismatch_row_ids),
        "duplicate_or_gap_row_ids": duplicate_or_gap,
    }
    trial_accounting_hash = canonical_sha256(
        {
            "report": report_for_hash,
            "attempts": [
                {
                    "row_id": a.row_id,
                    "experiment_id": a.experiment_id,
                    "retry_index": a.retry_index,
                    "status": a.status,
                }
                for a in normalized_attempts
            ],
        }
    )

    return CombinedAccountingReport(
        campaign_run_id=campaign_run_id,
        expected_total=EXPECTED_TOTAL_ROWS,
        registered_total=registered_total,
        primary_attempts=primary_attempts,
        total_attempts=total_attempts,
        retry_attempts=retry_attempts,
        status_counts=status_counts,
        missing_row_ids=tuple(missing),
        extra_experiment_ids=tuple(sorted(extra_experiment_ids)),
        mismatch_row_ids=tuple(sorted(mismatch_row_ids)),
        duplicate_or_gap_row_ids=tuple(duplicate_or_gap),
        accounting_complete=accounting_complete,
        all_primary_completed=all_primary_completed,
        performance_usable=performance_usable,
        trial_accounting_hash=trial_accounting_hash,
    )
