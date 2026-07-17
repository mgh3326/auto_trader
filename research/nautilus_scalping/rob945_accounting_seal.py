"""ROB-945 (H5, Task 1A) -- seal the exact frozen H4 campaign and the real
H6 accounting/attempt-evidence contract.

``seal_trial_accounting`` is the ONE pure boundary a caller (``rob945_scorecard
.build_scorecard``) must go through to trust H6 accounting evidence. It never
imports ``app.*``, never trusts a caller-supplied accounting hash, and never
accepts a merely self-consistent (but arbitrary) campaign -- ``full_campaign_hash``
is independently cross-checked against a FRESH, real
``rob944_frozen_campaign.build_production_frozen_campaign_envelope()`` (pure,
offline, no network/DB) rather than only checked for internal self-consistency
with a caller-supplied payload (hash-collision resistance means any
alternate/forged campaign payload cannot reproduce this pinned value).

ultrathink (material design decision): pinning ``full_campaign_hash`` against a
freshly recomputed real envelope -- rather than a hardcoded hex literal, and
rather than trusting the caller's OWN ``full_campaign_payload`` argument --
is what makes "reject a self-consistent arbitrary campaign" actually hold:
a caller could always construct an internally-consistent fake
payload/hash/run-id triple, but they cannot make it equal the ONE real
frozen envelope's hash without literally supplying the real campaign. This
also transitively re-validates dataset/signal-manifest hashes, the frozen
execution-code provenance, and the exact 24 experiment IDs/order -- all are
inputs to that one hash, so any single-field drift changes it.

Task 1A scope: structural validation only. ``fold_evidence_hash``/
``run_identity`` are validated here ONLY as opaque well-formed lowercase-hex64
strings; cross-binding their CONTENT against the real H4
``ConfigAttemptEvidenceSummary`` (via ``rob944_walkforward
.summarize_config_attempts_for_h6``) is Task 1B, layered on top of this module
without changing its existing public contract.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import rob944_frozen_campaign as frozen_campaign
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_DATA_GAP_IN_POSITION,
    REASON_GLOBAL_CORPUS_LOAD_FAILED,
    REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "ACCOUNTING_INCOMPLETE_REASON",
    "ACCOUNTING_REPORT_MALFORMED_REASON",
    "ATTEMPT_EVIDENCE_MALFORMED_REASON",
    "EXPECTED_PRIMARY_ATTEMPT_COUNT",
    "NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON",
    "PRIMARY_ATTEMPT_NOT_COMPLETED_REASON",
    "RETRIES_PRESENT_REASON",
    "ScorecardInputError",
    "SealedAttempt",
    "SealedScenarioEvidence",
    "SealedTrialAccounting",
    "derive_campaign_run_id",
    "seal_trial_accounting",
]

_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_PRIMARY_ATTEMPT_COUNT = 24
_CANONICAL_SCENARIO_ORDER = ("base", "primary_stress", "upward_stress")
_CLOSED_STATUSES = ("completed", "rejected", "crashed", "timeout")

# H4/H6-build-boundary status -> allowed-reason-code contract
# (run_rob944_campaign._attempt_allowed_reasons_by_status(), mirrored here).
_ALLOWED_REASONS_BY_STATUS: dict[str, frozenset[str]] = {
    "completed": frozenset(),  # must be exactly None -- checked separately
    "rejected": frozenset(
        {REASON_DATA_GAP_IN_POSITION, REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS}
    ),
    "crashed": frozenset(
        {REASON_CHILD_EXECUTION_CRASHED, REASON_GLOBAL_CORPUS_LOAD_FAILED}
    ),
    "timeout": frozenset({REASON_CHILD_EXECUTION_TIMEOUT}),
}

_REQUIRED_REPORT_FIELDS = (
    "campaign_run_id",
    "expected_total",
    "actual_registrations",
    "primary_attempts",
    "total_attempts",
    "retry_attempts",
    "status_counts",
    "missing_experiment_ids",
    "extra_experiment_ids",
    "mismatch_experiment_ids",
    "duplicate_or_gap_experiment_ids",
    "verdict",
)

NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON = (
    "full_campaign_hash_not_frozen_production_campaign"
)
ACCOUNTING_REPORT_MALFORMED_REASON = "h6_accounting_report_malformed"
ATTEMPT_EVIDENCE_MALFORMED_REASON = "h6_attempt_evidence_malformed"
ACCOUNTING_INCOMPLETE_REASON = "h6_accounting_incomplete"
PRIMARY_ATTEMPT_NOT_COMPLETED_REASON = "h6_primary_attempt_not_completed"
RETRIES_PRESENT_REASON = "h6_accounting_has_retries"


class ScorecardInputError(ValueError):
    """The sealed H5 evidence input failed a fail-closed boundary check."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ScorecardInputError(reason)


def derive_campaign_run_id(full_campaign_hash: str) -> str:
    """Bit-for-bit the SAME recipe as
    ``run_rob944_campaign._derive_primary_campaign_run_id`` /
    ``rob944_campaign_controller._derive_expected_campaign_run_id``: SHA-256
    of ``{"full_campaign_hash": ..., "kind": "primary_run"}`` -> raw 32
    bytes -> unpadded URL-safe base64 (43 chars) -> ``"rob944-primary-"``
    prefix -> 58 chars total. This is the single source of truth for the
    derivation -- ``rob945_scorecard`` imports it from here rather than
    keeping its own copy."""
    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    raw = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"rob944-primary-{suffix}"


@dataclass(frozen=True)
class SealedScenarioEvidence:
    scenario_name: str
    trade_count: int
    artifact_hash: str


@dataclass(frozen=True)
class SealedAttempt:
    campaign_run_id: str
    experiment_id: str
    retry_index: int
    status: str
    reason_code: str | None
    fold_evidence_hash: str
    run_identity: str
    scenario_evidence: tuple[
        SealedScenarioEvidence, SealedScenarioEvidence, SealedScenarioEvidence
    ]


@dataclass(frozen=True)
class SealedTrialAccounting:
    campaign_run_id: str
    full_campaign_hash: str
    report: Mapping[str, Any]
    attempts: tuple[SealedAttempt, ...]
    trial_accounting_hash: str
    accounting_complete: bool
    all_primary_completed: bool
    performance_usable: bool
    reason_codes: tuple[str, ...]


def _require_hex64(value: Any, reason: str) -> str:
    _require(isinstance(value, str) and bool(_LOWERCASE_HEX_64.match(value)), reason)
    return value


def _validate_report_shape(
    accounting_report: Any, *, expected_campaign_run_id: str
) -> dict[str, Any]:
    _require(isinstance(accounting_report, Mapping), ACCOUNTING_REPORT_MALFORMED_REASON)
    _require(
        set(accounting_report.keys()) == set(_REQUIRED_REPORT_FIELDS),
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    report = dict(accounting_report)

    _require(
        report["campaign_run_id"] == expected_campaign_run_id,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    for int_field in (
        "expected_total",
        "actual_registrations",
        "primary_attempts",
        "total_attempts",
        "retry_attempts",
    ):
        value = report[int_field]
        _require(type(value) is int and value >= 0, ACCOUNTING_REPORT_MALFORMED_REASON)
    _require(report["expected_total"] == 24, ACCOUNTING_REPORT_MALFORMED_REASON)

    status_counts = report["status_counts"]
    _require(
        isinstance(status_counts, Mapping)
        and set(status_counts.keys()) == set(_CLOSED_STATUSES),
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    for count in status_counts.values():
        _require(type(count) is int and count >= 0, ACCOUNTING_REPORT_MALFORMED_REASON)
    report["status_counts"] = dict(status_counts)

    for list_field in (
        "missing_experiment_ids",
        "extra_experiment_ids",
        "mismatch_experiment_ids",
        "duplicate_or_gap_experiment_ids",
    ):
        value = report[list_field]
        _require(
            isinstance(value, list) and all(isinstance(v, str) for v in value),
            ACCOUNTING_REPORT_MALFORMED_REASON,
        )
        report[list_field] = list(value)

    _require(
        report["verdict"] in ("complete", "incomplete"),
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    return report


def _validate_attempt(
    attempt: Any, *, expected_campaign_run_id: str, frozen_ids: frozenset[str]
) -> SealedAttempt:
    _require(isinstance(attempt, Mapping), ATTEMPT_EVIDENCE_MALFORMED_REASON)
    _require(
        set(attempt.keys())
        == {
            "attempt_key",
            "status",
            "reason_code",
            "fold_evidence_hash",
            "run_identity",
            "scenario_evidence",
        },
        ATTEMPT_EVIDENCE_MALFORMED_REASON,
    )

    attempt_key = attempt["attempt_key"]
    _require(isinstance(attempt_key, Mapping), ATTEMPT_EVIDENCE_MALFORMED_REASON)
    _require(
        set(attempt_key.keys()) == {"campaign_run_id", "experiment_id", "retry_index"},
        ATTEMPT_EVIDENCE_MALFORMED_REASON,
    )

    campaign_run_id = attempt_key["campaign_run_id"]
    _require(
        campaign_run_id == expected_campaign_run_id, ATTEMPT_EVIDENCE_MALFORMED_REASON
    )

    experiment_id = attempt_key["experiment_id"]
    _require(
        isinstance(experiment_id, str) and experiment_id in frozen_ids,
        ATTEMPT_EVIDENCE_MALFORMED_REASON,
    )

    retry_index = attempt_key["retry_index"]
    # type(...) is int (never isinstance) rejects bool (a bool subclasses
    # int in Python -- `isinstance(False, int)` is True, but `type(False)
    # is int` is False) and float uniformly.
    _require(
        type(retry_index) is int and retry_index >= 0, ATTEMPT_EVIDENCE_MALFORMED_REASON
    )

    status = attempt["status"]
    _require(status in _CLOSED_STATUSES, ATTEMPT_EVIDENCE_MALFORMED_REASON)
    reason_code = attempt["reason_code"]
    if status == "completed":
        _require(reason_code is None, ATTEMPT_EVIDENCE_MALFORMED_REASON)
    else:
        _require(
            reason_code in _ALLOWED_REASONS_BY_STATUS[status],
            ATTEMPT_EVIDENCE_MALFORMED_REASON,
        )

    fold_evidence_hash = _require_hex64(
        attempt["fold_evidence_hash"], ATTEMPT_EVIDENCE_MALFORMED_REASON
    )
    run_identity = _require_hex64(
        attempt["run_identity"], ATTEMPT_EVIDENCE_MALFORMED_REASON
    )

    scenario_evidence = attempt["scenario_evidence"]
    _require(
        isinstance(scenario_evidence, list | tuple) and len(scenario_evidence) == 3,
        ATTEMPT_EVIDENCE_MALFORMED_REASON,
    )
    sealed_scenarios: list[SealedScenarioEvidence] = []
    for expected_name, row in zip(
        _CANONICAL_SCENARIO_ORDER, scenario_evidence, strict=True
    ):
        _require(isinstance(row, Mapping), ATTEMPT_EVIDENCE_MALFORMED_REASON)
        _require(
            set(row.keys()) == {"scenario_name", "trade_count", "artifact_hash"},
            ATTEMPT_EVIDENCE_MALFORMED_REASON,
        )
        _require(
            row["scenario_name"] == expected_name, ATTEMPT_EVIDENCE_MALFORMED_REASON
        )
        trade_count = row["trade_count"]
        _require(
            type(trade_count) is int and trade_count >= 0,
            ATTEMPT_EVIDENCE_MALFORMED_REASON,
        )
        artifact_hash = _require_hex64(
            row["artifact_hash"], ATTEMPT_EVIDENCE_MALFORMED_REASON
        )
        sealed_scenarios.append(
            SealedScenarioEvidence(
                scenario_name=expected_name,
                trade_count=trade_count,
                artifact_hash=artifact_hash,
            )
        )

    return SealedAttempt(
        campaign_run_id=campaign_run_id,
        experiment_id=experiment_id,
        retry_index=retry_index,
        status=status,
        reason_code=reason_code,
        fold_evidence_hash=fold_evidence_hash,
        run_identity=run_identity,
        scenario_evidence=(
            sealed_scenarios[0],
            sealed_scenarios[1],
            sealed_scenarios[2],
        ),
    )


def _is_contiguous_from_zero(sorted_unique_indices: list[int]) -> bool:
    return sorted_unique_indices == list(range(len(sorted_unique_indices)))


def _scenario_to_plain(row: SealedScenarioEvidence) -> dict[str, Any]:
    return {
        "scenario_name": row.scenario_name,
        "trade_count": row.trade_count,
        "artifact_hash": row.artifact_hash,
    }


def _attempt_to_plain(attempt: SealedAttempt) -> dict[str, Any]:
    return {
        "campaign_run_id": attempt.campaign_run_id,
        "experiment_id": attempt.experiment_id,
        "retry_index": attempt.retry_index,
        "status": attempt.status,
        "reason_code": attempt.reason_code,
        "fold_evidence_hash": attempt.fold_evidence_hash,
        "run_identity": attempt.run_identity,
        "scenario_evidence": [_scenario_to_plain(s) for s in attempt.scenario_evidence],
    }


def seal_trial_accounting(
    *,
    accounting_report: Mapping[str, Any],
    attempt_evidence: Sequence[Mapping[str, Any]],
    full_campaign_hash: str,
) -> SealedTrialAccounting:
    """The one pure boundary for H6 accounting/attempt-evidence trust.

    Never trusts a caller-supplied ``full_campaign_hash`` at face value: it
    is compared against a FRESH, real ``build_production_frozen_campaign_envelope()``
    hash (never the caller's own claimed payload) -- see the module
    docstring's ``ultrathink`` note.
    """
    _require_hex64(full_campaign_hash, NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON)

    envelope = frozen_campaign.build_production_frozen_campaign_envelope()
    true_full_campaign_hash = envelope.full_campaign_hash()
    _require(
        full_campaign_hash == true_full_campaign_hash,
        NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON,
    )
    true_campaign_run_id = derive_campaign_run_id(true_full_campaign_hash)
    frozen_experiment_ids = tuple(envelope.to_dict()["experiment_ids"])
    _require(
        len(frozen_experiment_ids) == EXPECTED_PRIMARY_ATTEMPT_COUNT
        and len(set(frozen_experiment_ids)) == EXPECTED_PRIMARY_ATTEMPT_COUNT,
        NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON,
    )
    order_index = {eid: i for i, eid in enumerate(frozen_experiment_ids)}
    frozen_id_set = frozenset(frozen_experiment_ids)

    report = _validate_report_shape(
        accounting_report, expected_campaign_run_id=true_campaign_run_id
    )

    _require(
        isinstance(attempt_evidence, list | tuple), ATTEMPT_EVIDENCE_MALFORMED_REASON
    )
    sealed_attempts_raw = [
        _validate_attempt(
            a, expected_campaign_run_id=true_campaign_run_id, frozen_ids=frozen_id_set
        )
        for a in attempt_evidence
    ]

    _require(
        len(sealed_attempts_raw) == report["total_attempts"],
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )

    by_experiment: dict[str, list[SealedAttempt]] = {}
    for a in sealed_attempts_raw:
        by_experiment.setdefault(a.experiment_id, []).append(a)

    recomputed_missing = sorted(
        eid
        for eid in frozen_experiment_ids
        if 0 not in {r.retry_index for r in by_experiment.get(eid, [])}
    )
    recomputed_dup_or_gap = sorted(
        eid
        for eid, rows in by_experiment.items()
        if not _is_contiguous_from_zero(sorted({r.retry_index for r in rows}))
        or len(rows) != len({r.retry_index for r in rows})
    )
    recomputed_primary_attempts = sum(
        1 for rows in by_experiment.values() if any(r.retry_index == 0 for r in rows)
    )
    recomputed_actual_registrations = len(by_experiment)
    recomputed_status_counts = dict.fromkeys(_CLOSED_STATUSES, 0)
    for a in sealed_attempts_raw:
        recomputed_status_counts[a.status] += 1

    _require(
        report["missing_experiment_ids"] == recomputed_missing,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    # `extra_experiment_ids` can never be nonempty here: `_validate_attempt`
    # already rejects any experiment_id outside the frozen 24 as malformed.
    _require(report["extra_experiment_ids"] == [], ACCOUNTING_REPORT_MALFORMED_REASON)
    _require(
        sorted(report["duplicate_or_gap_experiment_ids"]) == recomputed_dup_or_gap,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["primary_attempts"] == recomputed_primary_attempts,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["actual_registrations"] == recomputed_actual_registrations,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["retry_attempts"]
        == len(sealed_attempts_raw) - recomputed_primary_attempts,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["status_counts"] == recomputed_status_counts,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )

    recomputed_complete = (
        not recomputed_missing
        and not report["extra_experiment_ids"]
        and not report["mismatch_experiment_ids"]
        and not recomputed_dup_or_gap
    )
    claimed_complete = report["verdict"] == "complete"
    _require(
        recomputed_complete == claimed_complete, ACCOUNTING_REPORT_MALFORMED_REASON
    )
    accounting_complete = recomputed_complete

    all_primary_completed = accounting_complete and all(
        any(
            r.retry_index == 0 and r.status == "completed"
            for r in by_experiment.get(eid, ())
        )
        for eid in frozen_experiment_ids
    )
    retry_attempts = report["retry_attempts"]
    performance_usable = (
        accounting_complete and all_primary_completed and retry_attempts == 0
    )

    reason_codes: list[str] = []
    if not accounting_complete:
        reason_codes.append(ACCOUNTING_INCOMPLETE_REASON)
    else:
        if not all_primary_completed:
            reason_codes.append(PRIMARY_ATTEMPT_NOT_COMPLETED_REASON)
        if all_primary_completed and retry_attempts > 0:
            reason_codes.append(RETRIES_PRESENT_REASON)

    normalized_attempts = tuple(
        sorted(
            sealed_attempts_raw,
            key=lambda a: (order_index[a.experiment_id], a.retry_index),
        )
    )

    trial_accounting_hash = canonical_sha256(
        {
            "report": report,
            "attempts": [_attempt_to_plain(a) for a in normalized_attempts],
        }
    )

    return SealedTrialAccounting(
        campaign_run_id=true_campaign_run_id,
        full_campaign_hash=full_campaign_hash,
        report=dict(report),
        attempts=normalized_attempts,
        trial_accounting_hash=trial_accounting_hash,
        accounting_complete=accounting_complete,
        all_primary_completed=all_primary_completed,
        performance_usable=performance_usable,
        reason_codes=tuple(reason_codes),
    )
