"""Independent, read-model-only ROB-974 R3 exact-12 post-audit kernel.

The future launcher may fetch these raw fields under a separate READ ONLY
transaction.  This module performs no I/O: it reconstructs exact-12 H6-A
accounting and refuses partial registration, retries, foreign rows, reported
count drift, and persisted attempt reordering.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import rob974_r3_accounting as r3_accounting
from rob974_h6a_accounting import AttemptAccountingRow
from rob974_r3_shape import Exact12MappingError, compute_exact_12_mapping_hash

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "R3PostAuditMismatch",
    "R3PostAuditRawSnapshot",
    "R3PostAuditSeal",
    "build_r3_postaudit_seal",
    "derive_r3_postaudit_campaign_run_id",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUS_ORDER: tuple[str, ...] = (
    "completed",
    "rejected",
    "crashed",
    "timeout",
)


class R3PostAuditMismatch(RuntimeError):
    """Raw R3 rows or reported accounting differ from the exact-12 plan."""


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise R3PostAuditMismatch(f"{name} must be lowercase 64-hex")
    return value


def derive_r3_postaudit_campaign_run_id(full_campaign_hash: str) -> str:
    """Independently reproduce the app-side R3 primary-run derivation."""

    _hex64(full_campaign_hash, "full_campaign_hash")
    digest = canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "kind": "rob974_r3_h6a_primary_run",
        }
    )
    suffix = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip("=")
    return f"rob974r3-{suffix}"


@dataclass(frozen=True, slots=True)
class R3PostAuditRawSnapshot:
    """One canonical raw-row projection, never pre-aggregated authority."""

    full_campaign_hash: str
    campaign_run_id: str
    registered_mapping: tuple[tuple[str, str], ...]
    attempts: tuple[AttemptAccountingRow, ...]
    reported_status_counts: tuple[tuple[str, int], ...]
    out_of_plan_experiment_ids: tuple[str, ...] = ()
    out_of_campaign_trial_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _hex64(self.full_campaign_hash, "snapshot full campaign hash")
        if type(self.campaign_run_id) is not str or not self.campaign_run_id:
            raise R3PostAuditMismatch(
                "snapshot campaign_run_id must be exact non-empty str"
            )
        if type(self.registered_mapping) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            for item in self.registered_mapping
        ):
            raise R3PostAuditMismatch(
                "snapshot registered mapping must contain exact string pairs"
            )
        if type(self.attempts) is not tuple or any(
            type(item) is not AttemptAccountingRow for item in self.attempts
        ):
            raise R3PostAuditMismatch(
                "snapshot attempts must use exact accounting-row tuples"
            )
        if type(self.reported_status_counts) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not int
            or item[1] < 0
            for item in self.reported_status_counts
        ):
            raise R3PostAuditMismatch(
                "reported status counts must be exact non-negative pairs"
            )
        for name in (
            "out_of_plan_experiment_ids",
            "out_of_campaign_trial_ids",
        ):
            value = getattr(self, name)
            if type(value) is not tuple or any(type(item) is not str for item in value):
                raise R3PostAuditMismatch(f"{name} must be an exact string tuple")


@dataclass(frozen=True, slots=True)
class R3PostAuditSeal:
    full_campaign_hash: str
    campaign_run_id: str
    exact_12_mapping_hash: str
    experiments: int
    trials: int
    strategy_counts: tuple[tuple[str, int], tuple[str, int]]
    primary_attempts: int
    total_attempts: int
    retry_attempts: int
    status_counts: tuple[tuple[str, int], ...]
    out_of_plan_experiments: int
    out_of_campaign_trials: int
    trial_accounting_hash: str


def build_r3_postaudit_seal(
    *,
    expected_full_campaign_hash: str,
    expected_campaign_run_id: str,
    expected_mapping: tuple[tuple[str, str], ...],
    snapshot: R3PostAuditRawSnapshot,
) -> R3PostAuditSeal:
    """Reconstruct exact-12 accounting solely from the supplied raw rows."""

    _hex64(expected_full_campaign_hash, "expected full campaign hash")
    if type(expected_campaign_run_id) is not str or not expected_campaign_run_id:
        raise R3PostAuditMismatch(
            "expected campaign_run_id must be exact non-empty str"
        )
    if expected_campaign_run_id != derive_r3_postaudit_campaign_run_id(
        expected_full_campaign_hash
    ):
        raise R3PostAuditMismatch(
            "expected campaign run ID is not independently R3-derived"
        )
    try:
        mapping_hash = compute_exact_12_mapping_hash(expected_mapping)
    except Exact12MappingError as exc:
        raise R3PostAuditMismatch(str(exc)) from exc
    if type(snapshot) is not R3PostAuditRawSnapshot:
        raise R3PostAuditMismatch("snapshot must use exact R3 raw-snapshot type")
    if snapshot.full_campaign_hash != expected_full_campaign_hash:
        raise R3PostAuditMismatch("raw rows carry the wrong full campaign hash")
    if snapshot.campaign_run_id != expected_campaign_run_id:
        raise R3PostAuditMismatch("raw rows carry the wrong campaign run ID")
    if snapshot.registered_mapping != expected_mapping:
        raise R3PostAuditMismatch(
            "registered mapping is partial, reordered, or out of plan"
        )
    if snapshot.out_of_plan_experiment_ids:
        raise R3PostAuditMismatch("out-of-plan experiment rows are present")
    if snapshot.out_of_campaign_trial_ids:
        raise R3PostAuditMismatch("out-of-campaign trial rows are present")
    try:
        accounting = r3_accounting.build_exact_12_accounting(
            campaign_run_id=expected_campaign_run_id,
            ordered_mapping=expected_mapping,
            registered_total=len(snapshot.registered_mapping),
            attempts=snapshot.attempts,
        )
    except r3_accounting.Exact12AccountingError as exc:
        raise R3PostAuditMismatch(str(exc)) from exc
    required = {
        "expected_total": 12,
        "registered_total": 12,
        "primary_attempts": 12,
        "total_attempts": 12,
        "retry_attempts": 0,
        "accounting_complete": True,
    }
    for name, expected in required.items():
        if getattr(accounting, name) != expected:
            raise R3PostAuditMismatch(f"R3 accounting field {name} is not exact")
    expected_counts = tuple(
        (status, accounting.status_counts[status]) for status in _STATUS_ORDER
    )
    if (
        snapshot.reported_status_counts != expected_counts
        or sum(count for _status, count in snapshot.reported_status_counts) != 12
    ):
        raise R3PostAuditMismatch("reported status-count surface is not exact 12")
    if any(
        getattr(accounting, name) != ()
        for name in (
            "missing_row_ids",
            "extra_experiment_ids",
            "mismatch_row_ids",
            "duplicate_or_gap_row_ids",
        )
    ):
        raise R3PostAuditMismatch("R3 accounting carries incomplete/foreign rows")
    row_ids = tuple(row_id for row_id, _experiment_id in expected_mapping)
    strategy_counts = (
        ("S3", sum(row_id.startswith("S3-R3-") for row_id in row_ids)),
        ("S4", sum(row_id.startswith("S4-R3-") for row_id in row_ids)),
    )
    if strategy_counts != (("S3", 3), ("S4", 9)):
        raise R3PostAuditMismatch("strategy experiment split must be exact 3+9")
    return R3PostAuditSeal(
        full_campaign_hash=expected_full_campaign_hash,
        campaign_run_id=expected_campaign_run_id,
        exact_12_mapping_hash=mapping_hash,
        experiments=12,
        trials=12,
        strategy_counts=strategy_counts,
        primary_attempts=accounting.primary_attempts,
        total_attempts=accounting.total_attempts,
        retry_attempts=accounting.retry_attempts,
        status_counts=expected_counts,
        out_of_plan_experiments=0,
        out_of_campaign_trials=0,
        trial_accounting_hash=accounting.trial_accounting_hash,
    )
