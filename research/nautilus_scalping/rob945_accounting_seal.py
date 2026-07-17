"""ROB-945 (H5, Task 1A+1B) -- seal the exact frozen H4 campaign and the
real H6 accounting/attempt-evidence contract.

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

Every primary (``retry_index == 0``) attempt's ``fold_evidence_hash``/
``run_identity``/status/reason/scenario evidence is additionally cross-bound
against the real H4 ``ConfigAttemptEvidenceSummary`` (via
``rob944_walkforward.summarize_config_attempts_for_h6``) derived from the
caller-supplied ``walkforward_results`` -- a structurally well-formed but
forged/stale attempt cannot pass merely because its fields are individually
well-typed hex64 strings.

Trust boundary: ``missing_experiment_ids``/``duplicate_or_gap_experiment_ids``
describe exactly what terminal evidence was/wasn't supplied and are fully,
independently recomputed from ``attempt_evidence`` and cross-checked.
``actual_registrations``/``extra_experiment_ids``/``mismatch_experiment_ids``
are H6 REGISTRATION-time facts (registration happens before any attempt
completes) this seal cannot observe from terminal evidence alone -- they are
validated for shape/domain-membership where knowable and hashed (tamper-
evident), never force-equated to a naive recompute from supplied attempts.
"""

from __future__ import annotations

import base64
import re
import types
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
    ConfigAttemptEvidenceSummary,
    ScenarioEvidenceSummary,
    WalkForwardResult,
    _json_safe_float_or_sentinel,
    summarize_config_attempts_for_h6,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "ACCOUNTING_INCOMPLETE_REASON",
    "ACCOUNTING_REPORT_MALFORMED_REASON",
    "ATTEMPT_EVIDENCE_MALFORMED_REASON",
    "CROSS_BIND_MISMATCH_REASON",
    "EXPECTED_PRIMARY_ATTEMPT_COUNT",
    "NOT_FROZEN_PRODUCTION_CAMPAIGN_REASON",
    "PRIMARY_ATTEMPT_NOT_COMPLETED_REASON",
    "RETRIES_PRESENT_REASON",
    "WALKFORWARD_RESULTS_MALFORMED_REASON",
    "ScorecardInputError",
    "SealedAttempt",
    "SealedScenarioEvidence",
    "SealedTrialAccounting",
    "derive_campaign_run_id",
    "seal_trial_accounting",
]

_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_PRIMARY_ATTEMPT_COUNT = 24
_EXPECTED_CONFIGS_PER_STRATEGY = 12
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
WALKFORWARD_RESULTS_MALFORMED_REASON = "walkforward_results_malformed"
CROSS_BIND_MISMATCH_REASON = "h4_cross_bind_evidence_mismatch"


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
    # The REAL, envelope-derived dataset/signal manifest hashes -- exposed
    # so a caller (``rob945_scorecard.build_scorecard``) can cross-check its
    # own ``dataset_manifest_hash``/``signal_manifest_hash`` arguments
    # against ground truth without a second, redundant envelope build.
    dataset_manifest_hash: str
    signal_manifest_hash: str
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
    accounting_report: Any, *, expected_campaign_run_id: str, frozen_ids: frozenset[str]
) -> dict[str, Any]:
    """Validates SHAPE/TYPE and, for the two REGISTRATION-time discrepancy
    lists this seal cannot independently observe from terminal
    ``attempt_evidence`` alone (``extra_experiment_ids``/
    ``mismatch_experiment_ids`` -- H6 registers all 24 identities BEFORE any
    attempt completes, so their presence/absence is trust-boundary data,
    never recomputed here), domain membership WHERE KNOWABLE: an ``extra``
    ID must NOT be one of the frozen 24 (by definition "extra" means outside
    the expected set); a ``mismatch`` ID MUST be one of the frozen 24 (it
    names an expected identity that drifted). ``missing_experiment_ids``/
    ``duplicate_or_gap_experiment_ids`` ARE independently recomputable from
    supplied attempts and are cross-checked for full content equality by the
    caller after this returns -- this function only validates their shape
    here. Every discrepancy list is normalized to sorted canonical order
    (order-insensitive input, order-deterministic hash output) and rejects
    within-list duplicate entries.
    """
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

    _DOMAIN_CHECKS = {
        "extra_experiment_ids": lambda eid: eid not in frozen_ids,
        "mismatch_experiment_ids": lambda eid: eid in frozen_ids,
    }
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
        _require(len(set(value)) == len(value), ACCOUNTING_REPORT_MALFORMED_REASON)
        domain_check = _DOMAIN_CHECKS.get(list_field)
        if domain_check is not None:
            _require(
                all(domain_check(eid) for eid in value),
                ACCOUNTING_REPORT_MALFORMED_REASON,
            )
        report[list_field] = sorted(value)

    # Task 1C (I1, extra_actual_25 / captain precision correction): a
    # single `mismatch` entry can itself correspond to MULTIPLE drifted
    # registered candidates sharing one params hash -- the serialized
    # report cannot reconstruct that multiplicity, so `actual_registrations`
    # has NO knowable upper bound here (never invent one; an arbitrarily
    # high value is a well-formed-incomplete registration fact, not
    # malformed). Only a LOWER bound is knowable and is enforced once
    # `by_experiment`/`extra_experiment_ids` are available, below.

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


# The one documented (status, reason_code) pairing that has no per-config
# WalkForwardResult to cross-bind against by construction: a global
# corpus-load failure is, by H4's own design, emitted identically for all
# 24 experiments with no per-config walk-forward ever having run at all.
_CROSS_BIND_EXEMPT_STATUS_REASON = ("crashed", REASON_GLOBAL_CORPUS_LOAD_FAILED)


def _global_failure_summary_for(
    strategy_key: str, config_id: str
) -> ConfigAttemptEvidenceSummary:
    """Mirrors ``run_rob944_campaign._global_failure_summaries``'s
    deterministic per-config sentinel byte-for-bit (known-vector parity,
    never importing ``app.*``) -- the ONLY legitimate shape for the
    whole-campaign ``global_corpus_load_failed`` fallback. Every primary's
    claimed fold_evidence_hash/run_identity is cross-bound against THIS,
    never freely accepted merely because its (status, reason_code) pair
    matches the sentinel."""
    slug = config_id.split("-", 1)[0]
    scenario_summaries = tuple(
        ScenarioEvidenceSummary(
            scenario_name=name,
            status="crashed",
            reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
            trade_count=0,
            artifact_hash=canonical_sha256(
                {
                    "strategy_key": strategy_key,
                    "config_id": config_id,
                    "scenario_name": name,
                    "status": "crashed",
                    "reason_code": REASON_GLOBAL_CORPUS_LOAD_FAILED,
                }
            ),
            no_trade_reason_counts={},
        )
        for name in _CANONICAL_SCENARIO_ORDER
    )
    return ConfigAttemptEvidenceSummary(
        strategy=slug,
        config_id=config_id,
        status="crashed",
        reason_code=REASON_GLOBAL_CORPUS_LOAD_FAILED,
        scenario_summaries=scenario_summaries,
    )


def _recompute_fold_evidence_hash_and_run_identity(
    summary: Any,
    *,
    full_campaign_hash: str,
    campaign_run_id: str,
    strategy_key: str,
    experiment_id: str,
    retry_index: int,
) -> tuple[str, str]:
    """Bit-for-bit the SAME recipe as the real H6-build boundary
    (``run_rob944_campaign._normalized_summary_to_attempt_evidence``) --
    pinned by a known-vector parity test rather than importing ``app.*``
    here. ``summary`` is a ``rob944_walkforward.ConfigAttemptEvidenceSummary``
    (a pure H4 sibling type, not ``app.*``)."""
    ordered_summaries = sorted(
        summary.scenario_summaries, key=lambda row: row.scenario_name
    )
    ordered_fold_trace = sorted(
        summary.fold_selection_trace, key=lambda row: row.fold_id
    )

    fold_evidence_hash = canonical_sha256(
        {
            "strategy": summary.strategy,
            "config_id": summary.config_id,
            "status": summary.status,
            "reason_code": summary.reason_code,
            "scenario_summaries": [
                {
                    "scenario_name": row.scenario_name,
                    "status": row.status,
                    "reason_code": row.reason_code,
                    "trade_count": row.trade_count,
                    "artifact_hash": row.artifact_hash,
                    "no_trade_reason_counts": row.no_trade_reason_counts,
                }
                for row in ordered_summaries
            ],
            "fold_selection_trace": [
                {
                    "fold_id": row.fold_id,
                    "fold_selected_config_id": row.fold_selected_config_id,
                    "eligible_symbols": list(row.eligible_symbols),
                    "excluded_symbols": [list(pair) for pair in row.excluded_symbols],
                    "equal_weight_expectancy_bps": _json_safe_float_or_sentinel(
                        row.equal_weight_expectancy_bps
                    ),
                    "pooled_expectancy_bps": _json_safe_float_or_sentinel(
                        row.pooled_expectancy_bps
                    ),
                    "profit_factor": _json_safe_float_or_sentinel(row.profit_factor),
                    "rejected": row.rejected,
                    "rejection_reason": row.rejection_reason,
                    "train_input_hash": row.train_input_hash,
                    "no_trade_reason_counts": row.no_trade_reason_counts,
                }
                for row in ordered_fold_trace
            ],
        }
    )
    run_identity = canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "strategy_key": strategy_key,
            "experiment_id": experiment_id,
            "retry_index": retry_index,
            "config_id": summary.config_id,
            "status": summary.status,
            "fold_evidence_hash": fold_evidence_hash,
        }
    )
    return fold_evidence_hash, run_identity


def _cross_bind_attempt(
    attempt: SealedAttempt,
    *,
    summary: Any,
    full_campaign_hash: str,
    campaign_run_id: str,
    strategy_key: str,
) -> None:
    recomputed_fold_hash, recomputed_run_identity = (
        _recompute_fold_evidence_hash_and_run_identity(
            summary,
            full_campaign_hash=full_campaign_hash,
            campaign_run_id=campaign_run_id,
            strategy_key=strategy_key,
            experiment_id=attempt.experiment_id,
            retry_index=attempt.retry_index,
        )
    )
    _require(
        attempt.status == summary.status and attempt.reason_code == summary.reason_code,
        CROSS_BIND_MISMATCH_REASON,
    )
    _require(
        attempt.fold_evidence_hash == recomputed_fold_hash, CROSS_BIND_MISMATCH_REASON
    )
    _require(
        attempt.run_identity == recomputed_run_identity, CROSS_BIND_MISMATCH_REASON
    )

    summary_scenarios_by_name = {
        row.scenario_name: row for row in summary.scenario_summaries
    }
    for scenario_row in attempt.scenario_evidence:
        real_row = summary_scenarios_by_name.get(scenario_row.scenario_name)
        _require(real_row is not None, CROSS_BIND_MISMATCH_REASON)
        _require(
            scenario_row.trade_count == real_row.trade_count
            and scenario_row.artifact_hash == real_row.artifact_hash,
            CROSS_BIND_MISMATCH_REASON,
        )


def _deep_freeze(obj: Any) -> Any:
    """Recursively converts dict/list into an immutable structure
    (``types.MappingProxyType`` + ``tuple``) -- a plain ``@dataclass(frozen=
    True)`` only blocks attribute REBINDING (``sealed.report = {...}``), it
    does nothing to a MUTABLE dict/list living inside an already-frozen
    dataclass field (``sealed.report["status_counts"]["completed"] = 999``
    would otherwise silently corrupt state the already-computed
    ``trial_accounting_hash`` claims to represent). Mirrors
    ``rob944_frozen_campaign._freeze``."""
    if isinstance(obj, dict):
        return types.MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, list | tuple):
        return tuple(_deep_freeze(v) for v in obj)
    return obj


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
    walkforward_results: Mapping[str, Any] | None = None,
) -> SealedTrialAccounting:
    """The one pure boundary for H6 accounting/attempt-evidence trust.

    Never trusts a caller-supplied ``full_campaign_hash`` at face value: it
    is compared against a FRESH, real ``build_production_frozen_campaign_envelope()``
    hash (never the caller's own claimed payload) -- see the module
    docstring's ``ultrathink`` note.

    ``walkforward_results`` (Task 1B) is normally exactly
    ``{"S1": WalkForwardResult, "S2": WalkForwardResult}`` -- every primary
    (``retry_index == 0``) attempt's opaque ``fold_evidence_hash``/
    ``run_identity``/status/reason/scenario evidence is cross-bound against
    the real H4 ``ConfigAttemptEvidenceSummary`` derived from it (via
    ``rob944_walkforward.summarize_config_attempts_for_h6``).

    ``walkforward_results=None`` (Task 1C, I4) is accepted ONLY as the
    genuine producer state for a whole-campaign
    ``global_corpus_load_failed`` fallback: H4 never even attempts a
    per-config walk-forward when the corpus itself never loaded, so there
    is, by construction, no real ``WalkForwardResult`` to pass -- a
    fabricated stand-in (e.g. an all-``crashed`` mapping) would not be the
    real producer state and the seal must never quietly accept one as
    equivalent. ``None`` is malformed unless ALL 24 primaries share the
    exact ``(crashed, global_corpus_load_failed)`` sentinel pairing (see
    ``_CROSS_BIND_EXEMPT_STATUS_REASON``); conversely, supplying a REAL
    ``walkforward_results`` mapping together with that same all-24 sentinel
    claim is itself a contradiction (a corpus that never loaded could never
    produce ANY real per-config H4 result) and is always rejected. In the
    ``None`` branch every primary is cross-bound against H4's deterministic
    fallback recipe (never a freely-accepted arbitrary hash), and the
    result is always performance-ineligible (no primary can carry
    ``status="completed"`` in this branch).
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
        accounting_report,
        expected_campaign_run_id=true_campaign_run_id,
        frozen_ids=frozen_id_set,
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

    by_experiment: dict[str, list[SealedAttempt]] = {}
    for a in sealed_attempts_raw:
        by_experiment.setdefault(a.experiment_id, []).append(a)

    experiment_id_to_config_id = dict(
        zip(frozen_experiment_ids, frozen_campaign.CANONICAL_ROW_ORDER, strict=True)
    )
    rows = envelope.to_dict()["rows"]
    experiment_id_to_strategy_key = dict(
        zip(frozen_experiment_ids, (row["strategy_key"] for row in rows), strict=True)
    )

    primary_rows = [a for a in sealed_attempts_raw if a.retry_index == 0]
    # Task 1C (I4): the global_corpus_load_failed exemption is legitimate
    # ONLY as the authentic whole-campaign fallback -- ALL 24 primaries
    # sharing the exact sentinel pairing, never an individual row while
    # others reflect real per-config H4 evidence (H4 emits this identically
    # for all 24 experiments when the corpus never loaded at all; a mixed
    # single-row sentinel is impossible by construction).
    claims_global_fallback = len(
        primary_rows
    ) == EXPECTED_PRIMARY_ATTEMPT_COUNT and all(
        (a.status, a.reason_code) == _CROSS_BIND_EXEMPT_STATUS_REASON
        for a in primary_rows
    )

    if walkforward_results is None:
        # Task 1C (I4, captain correction): `None` is the GENUINE producer
        # state when the corpus never loaded at all -- H4 never even
        # attempts a per-config walk-forward, so there is, by construction,
        # no real WalkForwardResult to pass. A fabricated stand-in (e.g. an
        # all-`crashed` mapping) is NOT that producer state and must never
        # be accepted as equivalent -- `None` is malformed unless the
        # supplied evidence is genuinely the all-24 fallback claim.
        _require(claims_global_fallback, WALKFORWARD_RESULTS_MALFORMED_REASON)
        for a in primary_rows:
            config_id = experiment_id_to_config_id[a.experiment_id]
            strategy_key = experiment_id_to_strategy_key[a.experiment_id]
            # Still byte-match H4's deterministic fallback recipe -- never
            # freely accept an arbitrary hash merely because the sentinel
            # pairing is present.
            summary = _global_failure_summary_for(strategy_key, config_id)
            _cross_bind_attempt(
                a,
                summary=summary,
                full_campaign_hash=true_full_campaign_hash,
                campaign_run_id=true_campaign_run_id,
                strategy_key=strategy_key,
            )
    else:
        # A corpus that never loaded could never produce ANY real
        # per-config H4 result -- supplying a real `walkforward_results`
        # mapping together with the all-24 global-fallback claim is always
        # a contradiction between the two forms of evidence the caller
        # itself supplied, and must be rejected outright (never silently
        # accepted merely because every row superficially matches the
        # sentinel pairing).
        _require(not claims_global_fallback, CROSS_BIND_MISMATCH_REASON)

        # Task 1B/1C (I5): mandatory cross-bind of EVERY normal-path
        # attempt's opaque evidence against the real H4
        # ConfigAttemptEvidenceSummary -- not just primaries. A retry
        # re-attempts the SAME config, so it is cross-bound against the
        # SAME summary as its primary; `_cross_bind_attempt` derives
        # `run_identity` using THAT row's own `retry_index` (which differs
        # from the primary's), so a forged/arbitrary hash on a retry row
        # can never be silently exempted just because cross-binding used to
        # apply only to `retry_index == 0`.
        _require(
            set(walkforward_results.keys()) == {"S1", "S2"},
            WALKFORWARD_RESULTS_MALFORMED_REASON,
        )
        summaries_by_strategy_config: dict[tuple[str, str], Any] = {}
        for strategy, wf_result in walkforward_results.items():
            # Task 1C (I3): the WalkForwardResult's OWN `.strategy` field
            # must match the dict slot it was supplied under -- without
            # this, a caller could supply a self-consistent (summary,
            # attempt) pair keyed under the WRONG strategy label and have
            # it cross-bind successfully, since the recompute would be
            # tautologically self-referential to that same mislabeled
            # summary. The exact frozen 12-config set (no duplicates, no
            # foreign-strategy config ids) is likewise required so a
            # duplicate/foreign config_id can never silently overwrite the
            # legitimate summary for its slot.
            _require(
                type(wf_result) is WalkForwardResult and wf_result.strategy == strategy,
                WALKFORWARD_RESULTS_MALFORMED_REASON,
            )
            summaries = tuple(summarize_config_attempts_for_h6(wf_result))
            config_ids_seen = [s.config_id for s in summaries]
            expected_config_ids = frozenset(
                f"{strategy}-{i:02d}" for i in range(_EXPECTED_CONFIGS_PER_STRATEGY)
            )
            _require(
                len(config_ids_seen) == _EXPECTED_CONFIGS_PER_STRATEGY
                and len(set(config_ids_seen)) == _EXPECTED_CONFIGS_PER_STRATEGY
                and set(config_ids_seen) == expected_config_ids
                # Belt-and-suspenders (captain correction): every summary's
                # OWN `.strategy` field must also match its S1/S2 slot, not
                # only the WalkForwardResult's outer `.strategy` field.
                and all(s.strategy == strategy for s in summaries),
                WALKFORWARD_RESULTS_MALFORMED_REASON,
            )
            for summary in summaries:
                summaries_by_strategy_config[(strategy, summary.config_id)] = summary

        for a in sealed_attempts_raw:
            config_id = experiment_id_to_config_id[a.experiment_id]
            strategy = config_id[:2]
            strategy_key = experiment_id_to_strategy_key[a.experiment_id]
            summary = summaries_by_strategy_config.get((strategy, config_id))
            _require(summary is not None, CROSS_BIND_MISMATCH_REASON)
            _cross_bind_attempt(
                a,
                summary=summary,
                full_campaign_hash=true_full_campaign_hash,
                campaign_run_id=true_campaign_run_id,
                strategy_key=strategy_key,
            )

    # `missing_experiment_ids`/`duplicate_or_gap_experiment_ids` describe
    # exactly what terminal evidence WAS/WASN'T supplied -- fully, exactly
    # recomputable from `sealed_attempts_raw` and `frozen_experiment_ids`
    # alone, independent of any H6 registration-time bookkeeping this seal
    # never observes. Both are ALWAYS strictly cross-checked (already
    # normalized to sorted order by `_validate_report_shape`).
    #
    # Task 1C (I1, authentic_mismatch): a frozen ID the caller has ALREADY,
    # independently classified as `mismatch` (a registration-time fact this
    # seal cannot recompute) is excluded from the missing-candidate set --
    # real H6 treats "missing" and "mismatch" as mutually exclusive
    # classifications of the same underlying registration/evidence gap, so
    # this seal must not ALSO reclassify a caller-trusted mismatch ID as
    # missing merely because no primary evidence happens to exist for it.
    mismatch_ids = frozenset(report["mismatch_experiment_ids"])
    # Captain consistency correction: real H6 only marks an ID `mismatch`
    # when the expected frozen registration is ABSENT -- it can therefore
    # never ALSO have terminal attempt evidence supplied under that same
    # frozen experiment_id (that would mean evidence exists for a
    # registration H6 itself says never happened as expected).
    _require(
        mismatch_ids.isdisjoint(by_experiment.keys()),
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    recomputed_missing = sorted(
        eid
        for eid in frozen_experiment_ids
        if eid not in mismatch_ids
        and 0 not in {r.retry_index for r in by_experiment.get(eid, [])}
    )

    # Task 1C (I1, authentic_retry_only): a group whose retry indices are
    # e.g. {1} alone (no primary, no literal duplicate) is simply MISSING
    # its primary -- already captured by `recomputed_missing` above -- and
    # must not ALSO be independently reclassified as duplicate/gap purely
    # because a non-zero-starting index isn't "contiguous from zero". A
    # genuine duplicate/gap requires either a literally repeated index, or
    # a real primary (index 0) present with a hole after it.
    def _is_duplicate_or_gap(rows: list) -> bool:
        indices = [r.retry_index for r in rows]
        unique_sorted = sorted(set(indices))
        if len(indices) != len(unique_sorted):
            return True
        if 0 not in unique_sorted:
            return False
        return not _is_contiguous_from_zero(unique_sorted)

    recomputed_dup_or_gap = sorted(
        eid for eid, rows in by_experiment.items() if _is_duplicate_or_gap(rows)
    )
    _require(
        report["missing_experiment_ids"] == recomputed_missing,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["duplicate_or_gap_experiment_ids"] == recomputed_dup_or_gap,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )

    # `actual_registrations` is a REGISTRATION-time fact (H6 predeclares all
    # identities before any attempt completes) this seal cannot observe from
    # terminal evidence -- only a LOWER bound is knowable, never an upper
    # one: a single `mismatch` entry can itself correspond to MULTIPLE
    # drifted registered candidates sharing one params hash, and every such
    # candidate inflates `actual_registrations` while entering neither
    # `by_experiment` (no terminal evidence) nor `extra_experiment_ids` (it
    # IS one of the frozen 24, just registered more than once) -- the
    # serialized report cannot reconstruct that multiplicity, so no finite
    # upper bound is ever enforced (captain precision correction, Task 1C
    # I1 appendix). The lower bound is the UNION of every ID this seal can
    # observe was registered: distinct supplied evidence + mismatch ids +
    # extra ids (a plain union already handles any overlap correctly).
    _require(
        len(
            set(by_experiment.keys())
            | mismatch_ids
            | frozenset(report["extra_experiment_ids"])
        )
        <= report["actual_registrations"],
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )

    # Task 1C (I1/I2, captain counter-parity correction): mirrors the real
    # H6 per-experiment loop exactly -- a group counts toward
    # primary/total/retry/status ONLY when its retry indices are a clean,
    # contiguous-from-zero sequence with no literal duplicate (a real
    # primary present, optionally followed by unbroken retries). Any other
    # group -- missing (no primary at all, e.g. a stray retry-only row),
    # duplicate, or gapped -- is excluded from these counters ENTIRELY
    # (H6 hits the anomaly, classifies it, and `continue`s without ever
    # tallying that group's rows); it is never counted at face value nor
    # silently absorbed into either accounting bucket. This uniformly
    # replaces any special-casing on whether a gap/duplicate happens to
    # exist anywhere in the campaign.
    def _is_clean_group(rows: list) -> bool:
        indices = [r.retry_index for r in rows]
        unique_sorted = sorted(set(indices))
        return len(indices) == len(unique_sorted) and _is_contiguous_from_zero(
            unique_sorted
        )

    clean_attempts = [
        a
        for a in sealed_attempts_raw
        if _is_clean_group(by_experiment[a.experiment_id])
    ]
    recomputed_total = len(clean_attempts)
    recomputed_primary = sum(
        1 for rows in by_experiment.values() if _is_clean_group(rows)
    )
    recomputed_status_counts = dict.fromkeys(_CLOSED_STATUSES, 0)
    for a in clean_attempts:
        recomputed_status_counts[a.status] += 1
    _require(
        report["total_attempts"] == recomputed_total, ACCOUNTING_REPORT_MALFORMED_REASON
    )
    _require(
        report["primary_attempts"] == recomputed_primary,
        ACCOUNTING_REPORT_MALFORMED_REASON,
    )
    _require(
        report["retry_attempts"] == recomputed_total - recomputed_primary,
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
        dataset_manifest_hash=envelope.dataset_manifest_hash,
        signal_manifest_hash=envelope.signal_manifest_hash,
        report=_deep_freeze(report),
        attempts=normalized_attempts,
        trial_accounting_hash=trial_accounting_hash,
        accounting_complete=accounting_complete,
        all_primary_completed=all_primary_completed,
        performance_usable=performance_usable,
        reason_codes=tuple(reason_codes),
    )
