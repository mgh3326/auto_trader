"""ROB-981 (ROB-974 R2 H6-A) CP3 -- attempt, eight-fold trace, dual-evidence
DTOs and semantic seals.

One logical PRIMARY attempt is one config's complete EIGHT-fold walk-forward
invocation (``fold-00..fold-07``); fold/unit/scenario children are nested
EVIDENCE inside that one attempt, never additional attempts (so a 48-config
campaign is always exactly 48 attempts -- never 384 (48x8) or 1152 (48x8x3)).

Two structurally distinct evidence kinds are kept apart BY CONSTRUCTION so
neither can be mistaken for the other:

  * ``UniqueGeneratorEvidence`` -- ONE scenario-independent generator
    candidate/rejection histogram per fold (H3 candidate identity is
    orthogonal to cost scenario -- a candidate is accepted/rejected once,
    not three times).
  * ``PathScenarioEvidence`` -- exactly THREE aggregate, attempt-level rows
    (``base13``/``primary_stress17``/``upward_stress22``), each committing
    its OWN membership -- summing/intersecting/reusing one path's evidence
    for another, or tripling unique evidence across scenarios, is refused by
    construction (distinct dataclasses, distinct fields, distinct hash
    recipes).

``fold_evidence_hash``/``run_identity`` are always INDEPENDENTLY RECOMPUTED
from trusted components at construction (``build_attempt_record``) -- a
caller-claimed value is a comparison input only, never trusted at face
value (mirrors ``rob945_accounting_seal``'s cross-bind discipline).

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
shared ``research_contracts.canonical_hash`` authority.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from research_contracts.canonical_hash import canonical_sha256


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """R1 blocker #4: ``frozen=True`` only blocks attribute REBINDING, not
    in-place mutation of a mutable dict the attribute happens to hold --
    this seals a validated str-keyed mapping into an immutable
    ``types.MappingProxyType`` snapshot (deep, non-aliasing copy) so a
    caller mutating it after construction raises instead of silently
    desyncing the sealed value from the hash already computed over it."""
    return types.MappingProxyType(dict(value))


__all__ = [
    "ALLOWED_REASONS_BY_STATUS",
    "ATTEMPT_STATUSES",
    "FOLD_COUNT",
    "FOLD_IDS",
    "GENERATOR_PHASES",
    "PATH_SCENARIOS",
    "REASON_CHILD_EXECUTION_CRASHED",
    "REASON_CHILD_EXECUTION_TIMEOUT",
    "REASON_DATA_GAP_IN_PAIR_POSITION",
    "REASON_DATA_GAP_IN_POSITION",
    "REASON_GLOBAL_CORPUS_LOAD_FAILED",
    "REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS",
    "AttemptEvidenceError",
    "AttemptRecord",
    "FoldSelectionTrace",
    "HistoricalExecutorState",
    "HashMismatchError",
    "PathScenarioEvidence",
    "UniqueGeneratorEvidence",
    "build_attempt_record",
]

FOLD_COUNT = 8
FOLD_IDS: tuple[str, ...] = tuple(f"fold-{i:02d}" for i in range(FOLD_COUNT))
PATH_SCENARIOS: tuple[str, ...] = ("base13", "primary_stress17", "upward_stress22")
GENERATOR_PHASES: tuple[str, ...] = (
    "train",
    "selected_oos",
    "pbo_full_window",
    "offline_smoke",
)

ATTEMPT_STATUSES: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")
_SCENARIO_STATUSES: tuple[str, ...] = (*ATTEMPT_STATUSES, "never_selected")

REASON_CHILD_EXECUTION_CRASHED = "child_execution_crashed"
REASON_CHILD_EXECUTION_TIMEOUT = "child_execution_timeout"
REASON_GLOBAL_CORPUS_LOAD_FAILED = "global_corpus_load_failed"
REASON_DATA_GAP_IN_POSITION = "rejected:data_gap_in_position"
REASON_DATA_GAP_IN_PAIR_POSITION = "rejected:data_gap_in_pair_position"
REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS = "insufficient_train_evidence_all_folds"

ALLOWED_REASONS_BY_STATUS: dict[str, frozenset[str]] = {
    "completed": frozenset(),  # must be exactly None -- checked separately
    "rejected": frozenset(
        {
            REASON_DATA_GAP_IN_POSITION,
            REASON_DATA_GAP_IN_PAIR_POSITION,
            REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
        }
    ),
    "crashed": frozenset(
        {REASON_CHILD_EXECUTION_CRASHED, REASON_GLOBAL_CORPUS_LOAD_FAILED}
    ),
    "timeout": frozenset({REASON_CHILD_EXECUTION_TIMEOUT}),
}
# never_selected is a SCENARIO-ONLY sentinel status -- it never appears in
# ALLOWED_REASONS_BY_STATUS (an attempt-level closed set) and can never be
# assigned as an attempt status (enforced in AttemptRecord.__post_init__).
_NEVER_SELECTED_REASON = None


class AttemptEvidenceError(ValueError):
    """Base error for the ROB-981 H6-A attempt/evidence DTOs."""


class HashMismatchError(AttemptEvidenceError):
    """A caller-claimed hash does not equal the value independently
    recomputed from trusted components -- caller-provided hashes are
    comparison inputs only, never authoritative."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AttemptEvidenceError(message)


def _require_exact_type(value: Any, expected: type, *, field: str) -> None:
    if type(value) is not expected:
        raise AttemptEvidenceError(
            f"{field} must be exact built-in {expected.__name__}, got {type(value).__name__}"
        )


def _is_hex64(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _require_hex64(value: Any, *, field: str) -> str:
    if not _is_hex64(value):
        raise AttemptEvidenceError(f"{field} must be a lowercase 64-hex SHA-256 digest")
    return value


@dataclass(frozen=True)
class FoldSelectionTrace:
    """One fold's TRAIN selection trace for one config -- present for EVERY
    fold regardless of whether this config won it (never a sparse/missing
    row for a losing fold)."""

    fold_id: str
    fold_index: int
    selected: bool
    eligible_symbols_or_pairs: tuple[str, ...]
    excluded_symbols_or_pairs: tuple[tuple[str, str], ...]  # (name, reason)
    accepted_input_hash: str | None
    rejection_reason: str | None
    no_trade_reason_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        _require_exact_type(self.fold_index, int, field="fold_index")
        _require(
            0 <= self.fold_index < FOLD_COUNT, f"fold_index must be in [0,{FOLD_COUNT})"
        )
        _require(
            self.fold_id == FOLD_IDS[self.fold_index],
            f"fold_id {self.fold_id!r} does not match fold_index {self.fold_index}",
        )
        _require_exact_type(self.selected, bool, field="selected")
        _require(
            isinstance(self.eligible_symbols_or_pairs, tuple)
            and all(type(s) is str for s in self.eligible_symbols_or_pairs),
            "eligible_symbols_or_pairs must be a tuple of str",
        )
        _require(
            isinstance(self.excluded_symbols_or_pairs, tuple)
            and all(
                type(pair) is tuple
                and len(pair) == 2
                and all(type(x) is str for x in pair)
                for pair in self.excluded_symbols_or_pairs
            ),
            "excluded_symbols_or_pairs must be a tuple of (name, reason) str pairs",
        )
        if self.accepted_input_hash is not None:
            _require_hex64(self.accepted_input_hash, field="accepted_input_hash")
        if not self.selected:
            _require(
                self.rejection_reason is not None or self.accepted_input_hash is None,
                "a non-selected fold with an accepted_input_hash must still carry a "
                "rejection_reason (it was evaluated and lost, not merely absent)",
            )
        _require(
            isinstance(self.no_trade_reason_counts, Mapping)
            and all(type(k) is str for k in self.no_trade_reason_counts)
            and all(
                type(v) is int and v >= 0 for v in self.no_trade_reason_counts.values()
            ),
            "no_trade_reason_counts must be a str->non-negative-int mapping",
        )
        object.__setattr__(
            self, "no_trade_reason_counts", _freeze_mapping(self.no_trade_reason_counts)
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "fold_index": self.fold_index,
            "selected": self.selected,
            "eligible_symbols_or_pairs": list(self.eligible_symbols_or_pairs),
            "excluded_symbols_or_pairs": [
                list(p) for p in self.excluded_symbols_or_pairs
            ],
            "accepted_input_hash": self.accepted_input_hash,
            "rejection_reason": self.rejection_reason,
            "no_trade_reason_counts": dict(self.no_trade_reason_counts),
        }


@dataclass(frozen=True)
class UniqueGeneratorEvidence:
    """ONE scenario-independent candidate/rejection histogram per fold, keyed
    by H3's canonical candidate identity. Cost scenario is never part of
    generator identity -- there is exactly one of these per fold, never one
    per (fold, scenario) triple."""

    fold_id: str
    phase: str
    candidate_identity_hash: str
    evaluated_decision_units: int
    no_signal: int
    no_signal_reason_histogram: Mapping[str, int]
    candidate: int
    generator_rejected: int
    generator_accepted: int
    generator_rejection_subtotal_by_reason: Mapping[str, int]
    content_hash: str

    def __post_init__(self) -> None:
        _require(
            self.fold_id in FOLD_IDS, f"fold_id {self.fold_id!r} is not a known fold"
        )
        _require(
            self.phase in GENERATOR_PHASES,
            f"phase {self.phase!r} is not a known generator phase",
        )
        _require_hex64(self.candidate_identity_hash, field="candidate_identity_hash")
        for name in (
            "evaluated_decision_units",
            "no_signal",
            "candidate",
            "generator_rejected",
            "generator_accepted",
        ):
            value = getattr(self, name)
            _require_exact_type(value, int, field=name)
            _require(value >= 0, f"{name} must be non-negative")
        _require(
            self.evaluated_decision_units == self.no_signal + self.candidate,
            "evaluated_decision_units must equal no_signal + candidate",
        )
        _require(
            isinstance(self.no_signal_reason_histogram, Mapping)
            and all(type(k) is str for k in self.no_signal_reason_histogram)
            and all(
                type(v) is int and v >= 0
                for v in self.no_signal_reason_histogram.values()
            ),
            "no_signal_reason_histogram must be a str->non-negative-int mapping",
        )
        _require(
            sum(self.no_signal_reason_histogram.values()) == self.no_signal,
            "no_signal_reason_histogram must sum to no_signal",
        )
        _require(
            self.candidate == self.generator_rejected + self.generator_accepted,
            "candidate must equal generator_rejected + generator_accepted",
        )
        _require(
            isinstance(self.generator_rejection_subtotal_by_reason, Mapping)
            and all(type(k) is str for k in self.generator_rejection_subtotal_by_reason)
            and all(
                type(v) is int and v >= 0
                for v in self.generator_rejection_subtotal_by_reason.values()
            ),
            "generator_rejection_subtotal_by_reason must be a str->non-negative-int mapping",
        )
        _require(
            sum(self.generator_rejection_subtotal_by_reason.values())
            == self.generator_rejected,
            "generator_rejection_subtotal_by_reason must sum to generator_rejected",
        )
        object.__setattr__(
            self,
            "no_signal_reason_histogram",
            _freeze_mapping(self.no_signal_reason_histogram),
        )
        object.__setattr__(
            self,
            "generator_rejection_subtotal_by_reason",
            _freeze_mapping(self.generator_rejection_subtotal_by_reason),
        )
        recomputed = _recompute_unique_evidence_hash(self)
        if recomputed != self.content_hash:
            raise HashMismatchError(
                f"UniqueGeneratorEvidence[{self.fold_id}]: content_hash does not match the "
                "value independently recomputed from trusted fields"
            )


def _recompute_unique_evidence_hash(evidence: UniqueGeneratorEvidence) -> str:
    return canonical_sha256(
        {
            "fold_id": evidence.fold_id,
            "phase": evidence.phase,
            "candidate_identity_hash": evidence.candidate_identity_hash,
            "evaluated_decision_units": evidence.evaluated_decision_units,
            "no_signal": evidence.no_signal,
            "no_signal_reason_histogram": dict(evidence.no_signal_reason_histogram),
            "candidate": evidence.candidate,
            "generator_rejected": evidence.generator_rejected,
            "generator_accepted": evidence.generator_accepted,
            "generator_rejection_subtotal_by_reason": dict(
                evidence.generator_rejection_subtotal_by_reason
            ),
        }
    )


@dataclass(frozen=True)
class PathScenarioEvidence:
    """One of exactly THREE attempt-level aggregate cost-scenario rows.

    ``status="never_selected"`` is the canonical sentinel for a config that
    won no fold -- ``trade_count=0``, no member trades, no reason_code. This
    status is legal ONLY here, never on ``AttemptRecord.status``.

    ``artifact_hash`` commits ``path_scenario`` AND ``member_trade_keys`` --
    a trade row's counterfactual E13/E17/E22 columns elsewhere are never
    treated as membership evidence for a DIFFERENT scenario; this hash is
    the one place membership is actually committed.
    """

    path_scenario: str
    status: str
    reason_code: str | None
    trade_count: int
    member_trade_keys: tuple[str, ...]
    no_trade_reason_counts: Mapping[str, int]
    artifact_hash: str

    def __post_init__(self) -> None:
        _require(
            self.path_scenario in PATH_SCENARIOS,
            f"path_scenario must be one of {PATH_SCENARIOS}",
        )
        _require(
            self.status in _SCENARIO_STATUSES,
            f"status must be one of {_SCENARIO_STATUSES}",
        )
        _require_exact_type(self.trade_count, int, field="trade_count")
        _require(self.trade_count >= 0, "trade_count must be non-negative")
        _require(
            isinstance(self.member_trade_keys, tuple)
            and all(_is_hex64(k) for k in self.member_trade_keys),
            "member_trade_keys must be a tuple of hex64 keys",
        )
        _require(
            len(set(self.member_trade_keys)) == len(self.member_trade_keys),
            "member_trade_keys must not contain duplicates",
        )
        _require(
            self.trade_count == len(self.member_trade_keys),
            "trade_count must equal len(member_trade_keys)",
        )
        if self.status == "never_selected":
            _require(self.trade_count == 0, "never_selected must carry trade_count=0")
            _require(
                self.reason_code is None,
                "never_selected must carry reason_code=None (it is a sentinel, not a "
                "generator-rejection or funding/engine reason)",
            )
        elif self.status == "completed":
            _require(self.reason_code is None, "completed must carry reason_code=None")
        else:
            allowed = ALLOWED_REASONS_BY_STATUS.get(self.status, frozenset())
            _require(
                self.reason_code in allowed,
                f"reason_code {self.reason_code!r} is not permitted for status "
                f"{self.status!r} under the closed allowlist",
            )
        _require(
            isinstance(self.no_trade_reason_counts, Mapping)
            and all(type(k) is str for k in self.no_trade_reason_counts)
            and all(
                type(v) is int and v >= 0 for v in self.no_trade_reason_counts.values()
            ),
            "no_trade_reason_counts must be a str->non-negative-int mapping",
        )
        object.__setattr__(
            self, "no_trade_reason_counts", _freeze_mapping(self.no_trade_reason_counts)
        )
        recomputed = _recompute_path_scenario_hash(self)
        if recomputed != self.artifact_hash:
            raise HashMismatchError(
                f"PathScenarioEvidence[{self.path_scenario}]: artifact_hash does not match "
                "the value independently recomputed from trusted fields"
            )


def _recompute_path_scenario_hash(evidence: PathScenarioEvidence) -> str:
    return canonical_sha256(
        {
            "path_scenario": evidence.path_scenario,
            "status": evidence.status,
            "reason_code": evidence.reason_code,
            "trade_count": evidence.trade_count,
            "member_trade_keys": sorted(evidence.member_trade_keys),
            "no_trade_reason_counts": dict(evidence.no_trade_reason_counts),
        }
    )


@dataclass(frozen=True)
class HistoricalExecutorState:
    """S4 "completed" means basket SIMULATION completed only. Every
    operational field is a fixed, non-observed sentinel -- there is no way
    to construct this dataclass with an actual order ID, a
    executor-validated True, or a numeric ``pair_exec_fail`` (only the
    literal string ``"not_evaluated"`` is accepted)."""

    order_id: None = None
    executor_validated: None = None
    pair_exec_fail: Literal["not_evaluated"] = "not_evaluated"
    demo_eligible: bool = False
    promotion_blocked_reason: str = "promotion_blocked_pending_pair_executor"

    def __post_init__(self) -> None:
        _require(
            self.order_id is None, "order_id must be null (historical, never observed)"
        )
        _require(
            self.executor_validated is None,
            "executor_validated must be null (never observed/atomic-fill success)",
        )
        _require(
            self.pair_exec_fail == "not_evaluated",
            "pair_exec_fail must be the literal 'not_evaluated' sentinel -- a numeric "
            "0/False is never observed evidence",
        )
        _require_exact_type(self.demo_eligible, bool, field="demo_eligible")
        _require(
            self.demo_eligible is False,
            "demo_eligible must be False -- even a historical PASS remains "
            "promotion_blocked_pending_pair_executor",
        )


@dataclass(frozen=True)
class AttemptRecord:
    """One logical PRIMARY attempt -- one config's complete eight-fold
    invocation. ``fold_evidence_hash``/``run_identity`` are the CLAIMED
    values; use :func:`build_attempt_record` to construct one with
    INDEPENDENTLY RECOMPUTED hashes (never trust a caller-passed value
    directly)."""

    row_id: str
    experiment_id: str
    campaign_run_id: str
    full_campaign_hash: str
    strategy_key: str
    retry_index: int
    status: str
    reason_code: str | None
    fold_traces: tuple[FoldSelectionTrace, ...]
    unique_evidence: tuple[UniqueGeneratorEvidence, ...]
    path_scenario_evidence: tuple[
        PathScenarioEvidence, PathScenarioEvidence, PathScenarioEvidence
    ]
    fold_evidence_hash: str
    run_identity: str
    historical_executor_state: HistoricalExecutorState | None = None

    def __post_init__(self) -> None:
        _require_hex64(self.experiment_id, field="experiment_id")
        _require_exact_type(self.retry_index, int, field="retry_index")
        _require(self.retry_index >= 0, "retry_index must be non-negative")
        _require(
            self.status in ATTEMPT_STATUSES,
            f"status must be one of {ATTEMPT_STATUSES} -- 'never_selected' is scenario-only "
            "and can never be an attempt status",
        )
        if self.status == "completed":
            _require(self.reason_code is None, "completed must carry reason_code=None")
        else:
            allowed = ALLOWED_REASONS_BY_STATUS[self.status]
            _require(
                self.reason_code in allowed,
                f"reason_code {self.reason_code!r} is not permitted for status "
                f"{self.status!r} under the closed allowlist",
            )

        _require(
            isinstance(self.fold_traces, tuple) and len(self.fold_traces) == FOLD_COUNT,
            f"fold_traces must be a tuple of exactly {FOLD_COUNT} entries",
        )
        seen_indices = [trace.fold_index for trace in self.fold_traces]
        _require(
            seen_indices == list(range(FOLD_COUNT)),
            "fold_traces must be present for every fold, in ascending fold_index order, "
            "no duplicate/missing/reordered fold",
        )

        _require(
            isinstance(self.unique_evidence, tuple)
            and len(self.unique_evidence) == FOLD_COUNT,
            f"unique_evidence must be a tuple of exactly {FOLD_COUNT} entries (one per fold, "
            "scenario-independent -- never tripled across scenarios)",
        )
        unique_fold_ids = [ev.fold_id for ev in self.unique_evidence]
        _require(
            unique_fold_ids == list(FOLD_IDS),
            "unique_evidence must cover every fold, in canonical fold order, exactly once",
        )
        for trace, evidence in zip(self.fold_traces, self.unique_evidence, strict=True):
            if trace.selected:
                _require(
                    evidence.phase == "selected_oos",
                    "a selected fold must carry selected_oos unique evidence",
                )

        _require(
            isinstance(self.path_scenario_evidence, tuple)
            and len(self.path_scenario_evidence) == 3,
            "path_scenario_evidence must be a tuple of exactly 3 entries",
        )
        scenario_names = tuple(ev.path_scenario for ev in self.path_scenario_evidence)
        _require(
            scenario_names == PATH_SCENARIOS,
            f"path_scenario_evidence must be in the exact canonical order {PATH_SCENARIOS}",
        )

        any_fold_selected = any(trace.selected for trace in self.fold_traces)
        if self.status == "completed" and not any_fold_selected:
            # TRAIN-eligible config that won no fold -- attempt may still be
            # "completed", but every scenario must carry the never_selected
            # sentinel (a config that never won a fold cannot have real OOS
            # trades in any scenario).
            _require(
                all(
                    ev.status == "never_selected" for ev in self.path_scenario_evidence
                ),
                "a completed attempt with no selected fold must carry never_selected in "
                "every path_scenario_evidence row",
            )
        if any_fold_selected:
            _require(
                all(
                    ev.status != "never_selected" for ev in self.path_scenario_evidence
                ),
                "an attempt with at least one selected fold cannot carry a never_selected "
                "path_scenario_evidence row",
            )

        strategy_slug = self.row_id.split("-", 1)[0]
        if strategy_slug == "S4":
            if self.status == "completed":
                _require(
                    self.historical_executor_state is not None,
                    "a completed S4 attempt must carry a historical_executor_state",
                )
            _require(
                self.historical_executor_state is None
                or type(self.historical_executor_state) is HistoricalExecutorState,
                "historical_executor_state must be an exact HistoricalExecutorState instance",
            )
        elif strategy_slug == "S3":
            _require(
                self.historical_executor_state is None,
                "S3 attempts never carry a historical_executor_state (pair-executor concept "
                "is S4-only)",
            )
        else:
            raise AttemptEvidenceError(
                f"row_id {self.row_id!r} has an unknown strategy slug"
            )

        recomputed_fold_hash = _recompute_fold_evidence_hash(self)
        if recomputed_fold_hash != self.fold_evidence_hash:
            raise HashMismatchError(
                f"AttemptRecord[{self.row_id}]: fold_evidence_hash does not match the value "
                "independently recomputed from trusted fields"
            )
        recomputed_run_identity = _recompute_run_identity(self, recomputed_fold_hash)
        if recomputed_run_identity != self.run_identity:
            raise HashMismatchError(
                f"AttemptRecord[{self.row_id}]: run_identity does not match the value "
                "independently recomputed from trusted fields"
            )


def _historical_executor_state_payload(
    state: HistoricalExecutorState | None,
) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "order_id": state.order_id,
        "executor_validated": state.executor_validated,
        "pair_exec_fail": state.pair_exec_fail,
        "demo_eligible": state.demo_eligible,
        "promotion_blocked_reason": state.promotion_blocked_reason,
    }


def _fold_evidence_payload(
    *,
    row_id: str,
    status: str,
    reason_code: str | None,
    fold_traces: tuple[FoldSelectionTrace, ...],
    unique_evidence: tuple[UniqueGeneratorEvidence, ...],
    path_scenario_evidence: tuple[PathScenarioEvidence, ...],
    historical_executor_state: HistoricalExecutorState | None,
) -> dict[str, Any]:
    """The ONE canonical payload shape ``fold_evidence_hash`` commits --
    shared by :func:`build_attempt_record` (the trusted-boundary recompute)
    and ``AttemptRecord.__post_init__`` (the construction-time re-verify),
    so the two can never silently drift apart."""
    return {
        "row_id": row_id,
        "status": status,
        "reason_code": reason_code,
        "fold_traces": [trace.canonical_payload() for trace in fold_traces],
        "unique_evidence": [
            {
                "fold_id": ev.fold_id,
                "phase": ev.phase,
                "candidate_identity_hash": ev.candidate_identity_hash,
                "evaluated_decision_units": ev.evaluated_decision_units,
                "no_signal": ev.no_signal,
                "no_signal_reason_histogram": dict(ev.no_signal_reason_histogram),
                "candidate": ev.candidate,
                "generator_rejected": ev.generator_rejected,
                "generator_accepted": ev.generator_accepted,
                "generator_rejection_subtotal_by_reason": dict(
                    ev.generator_rejection_subtotal_by_reason
                ),
            }
            for ev in unique_evidence
        ],
        "path_scenario_evidence": [
            {
                "path_scenario": ev.path_scenario,
                "status": ev.status,
                "reason_code": ev.reason_code,
                "trade_count": ev.trade_count,
                "member_trade_keys": sorted(ev.member_trade_keys),
                "no_trade_reason_counts": dict(ev.no_trade_reason_counts),
            }
            for ev in path_scenario_evidence
        ],
        "historical_executor_state": _historical_executor_state_payload(
            historical_executor_state
        ),
    }


def _recompute_fold_evidence_hash(attempt: AttemptRecord) -> str:
    return canonical_sha256(
        _fold_evidence_payload(
            row_id=attempt.row_id,
            status=attempt.status,
            reason_code=attempt.reason_code,
            fold_traces=attempt.fold_traces,
            unique_evidence=attempt.unique_evidence,
            path_scenario_evidence=attempt.path_scenario_evidence,
            historical_executor_state=attempt.historical_executor_state,
        )
    )


def _recompute_run_identity(attempt: AttemptRecord, fold_evidence_hash: str) -> str:
    return canonical_sha256(
        {
            "full_campaign_hash": attempt.full_campaign_hash,
            "campaign_run_id": attempt.campaign_run_id,
            "strategy_key": attempt.strategy_key,
            "experiment_id": attempt.experiment_id,
            "retry_index": attempt.retry_index,
            "row_id": attempt.row_id,
            "status": attempt.status,
            "fold_evidence_hash": fold_evidence_hash,
        }
    )


def build_attempt_record(
    *,
    row_id: str,
    experiment_id: str,
    campaign_run_id: str,
    full_campaign_hash: str,
    strategy_key: str,
    retry_index: int,
    status: str,
    reason_code: str | None,
    fold_traces: tuple[FoldSelectionTrace, ...],
    unique_evidence: tuple[UniqueGeneratorEvidence, ...],
    path_scenario_evidence: tuple[
        PathScenarioEvidence, PathScenarioEvidence, PathScenarioEvidence
    ],
    historical_executor_state: HistoricalExecutorState | None = None,
    claimed_fold_evidence_hash: str | None = None,
    claimed_run_identity: str | None = None,
) -> AttemptRecord:
    """Build one attempt with INDEPENDENTLY RECOMPUTED
    ``fold_evidence_hash``/``run_identity`` -- ``claimed_*`` values, if
    given, are compared-only inputs: a mismatch raises before construction
    completes, and the returned record always carries the recomputed
    (never the caller-claimed) value."""
    fold_evidence_hash = canonical_sha256(
        _fold_evidence_payload(
            row_id=row_id,
            status=status,
            reason_code=reason_code,
            fold_traces=fold_traces,
            unique_evidence=unique_evidence,
            path_scenario_evidence=path_scenario_evidence,
            historical_executor_state=historical_executor_state,
        )
    )
    if (
        claimed_fold_evidence_hash is not None
        and claimed_fold_evidence_hash != fold_evidence_hash
    ):
        raise HashMismatchError(
            f"AttemptRecord[{row_id}]: claimed fold_evidence_hash does not match the "
            "independently recomputed value"
        )

    run_identity = canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "strategy_key": strategy_key,
            "experiment_id": experiment_id,
            "retry_index": retry_index,
            "row_id": row_id,
            "status": status,
            "fold_evidence_hash": fold_evidence_hash,
        }
    )
    if claimed_run_identity is not None and claimed_run_identity != run_identity:
        raise HashMismatchError(
            f"AttemptRecord[{row_id}]: claimed run_identity does not match the independently "
            "recomputed value"
        )

    return AttemptRecord(
        row_id=row_id,
        experiment_id=experiment_id,
        campaign_run_id=campaign_run_id,
        full_campaign_hash=full_campaign_hash,
        strategy_key=strategy_key,
        retry_index=retry_index,
        status=status,
        reason_code=reason_code,
        fold_traces=fold_traces,
        unique_evidence=unique_evidence,
        path_scenario_evidence=path_scenario_evidence,
        fold_evidence_hash=fold_evidence_hash,
        run_identity=run_identity,
        historical_executor_state=historical_executor_state,
    )
