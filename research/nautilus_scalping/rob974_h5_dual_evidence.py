"""ROB-983 (H5, CP2) -- selected-OOS dual evidence and PBO prerequisites.

Two distinct typed evidence surfaces per H4/H6-A contract (H4 AC30-33,
H6-A AC20-21):

* ``UniqueGeneratorEvidence`` -- scenario-independent, pre-horizon/
  pre-funding/pre-engine H3 candidate identity, keyed once per
  strategy/config/fold. ``candidate = accepted + rejected``; the rejection
  reason histogram subtotals exactly to ``rejected``.
* ``PathInvocationEvidence`` -- one row per ``path_scenario``
  (base13/primary_stress17/upward_stress22), each referencing the SAME
  unique accepted-input hash/count and carrying its own post-horizon/funding
  engine-input hash/count and no-trade reasons.

``cross_check_dual_evidence`` is the one boundary that proves every path
traces back to the SAME unique accepted set -- never a scenario sum,
intersection, first-path substitute, or tripled count.

PBO (``PboEvidence``) is exact independent 24-config x 365-day,
``primary_stress17``, ``slices=4``, full-window, reference-only auxiliary
evidence. It never enters a hard gate: ``validate_pbo_evidence`` returns only
an ok/incomplete-reason pair, never a verdict-shaped value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rob974_h5_contracts import (
    FAKE_FREE_EMPIRICAL_CLOSURE,
    FOLD_IDS,
    PATH_SCENARIOS,
    STRATEGIES,
    EnvelopeValidationResult,
    H4AttributionContractResult,
    H5InputError,
    config_ids_for,
)

_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise H5InputError(reason)


def _require_hex64(value: Any, reason: str) -> str:
    _require(isinstance(value, str) and bool(_LOWERCASE_HEX_64.match(value)), reason)
    return value


def _require_exact_int(value: Any, reason: str, *, min_value: int = 0) -> int:
    _require(type(value) is int and value >= min_value, reason)
    return value


__all__ = [
    "PBO_CONFIG_COUNT",
    "PBO_DAY_COUNT",
    "PBO_SCENARIO_NAME",
    "PBO_SLICES",
    "PathInvocationEvidence",
    "PboEvidence",
    "UniqueGeneratorEvidence",
    "H4AttributionCrossCheck",
    "cross_check_h4_attribution_contract",
    "cross_check_dual_evidence",
    "validate_pbo_evidence",
]

PBO_CONFIG_COUNT = 24
PBO_DAY_COUNT = 365
PBO_SLICES = 4
PBO_SCENARIO_NAME = "primary_stress17"
GENERATOR_PHASES = ("train", "selected_oos", "pbo_full_window", "offline_smoke")


def _require_reason_histogram(
    value: Any, expected_total: int, reason: str
) -> dict[str, int]:
    _require(isinstance(value, dict), reason)
    for k, v in value.items():
        _require(isinstance(k, str), reason)
        _require_exact_int(v, reason, min_value=1)
    _require(sum(value.values()) == expected_total, reason)
    return dict(value)


@dataclass(frozen=True, slots=True)
class UniqueGeneratorEvidence:
    strategy: str
    config_id: str
    fold_id: str
    phase: str
    evaluated_decision_units: int
    no_signal: int
    no_signal_reason_histogram: dict[str, int]
    accepted: int
    rejected: int
    accepted_input_hash: str
    rejection_reason_histogram: dict[str, int]

    def __post_init__(self) -> None:
        _require(self.strategy in STRATEGIES, "unique_evidence_strategy_unknown")
        _require(
            self.config_id in config_ids_for(self.strategy),
            "unique_evidence_config_id_unknown",
        )
        _require(self.fold_id in FOLD_IDS, "unique_evidence_fold_id_unknown")
        _require(self.phase in GENERATOR_PHASES, "unique_evidence_phase_unknown")
        _require_exact_int(
            self.evaluated_decision_units,
            "unique_evidence_evaluated_decision_units_malformed",
        )
        _require_exact_int(self.no_signal, "unique_evidence_no_signal_malformed")
        _require_reason_histogram(
            self.no_signal_reason_histogram,
            self.no_signal,
            "unique_evidence_no_signal_histogram_subtotal_mismatch",
        )
        _require_exact_int(self.accepted, "unique_evidence_accepted_malformed")
        _require_exact_int(self.rejected, "unique_evidence_rejected_malformed")
        _require(
            self.evaluated_decision_units == self.no_signal + self.candidate,
            "unique_evidence_evaluated_partition_mismatch",
        )
        _require_hex64(
            self.accepted_input_hash, "unique_evidence_accepted_input_hash_malformed"
        )
        _require_reason_histogram(
            self.rejection_reason_histogram,
            self.rejected,
            "unique_evidence_rejection_histogram_subtotal_mismatch",
        )

    @property
    def candidate(self) -> int:
        return self.accepted + self.rejected


@dataclass(frozen=True, slots=True)
class PathInvocationEvidence:
    strategy: str
    config_id: str
    fold_id: str
    path_scenario: str
    unique_evidence_hash: str
    unique_evidence_accepted_count: int
    engine_input_hash: str
    engine_input_count: int
    no_trade_reason_counts: dict[str, int]
    ledger_status: str
    trade_count: int
    artifact_hash: str

    def __post_init__(self) -> None:
        _require(self.strategy in STRATEGIES, "path_evidence_strategy_unknown")
        _require(
            self.config_id in config_ids_for(self.strategy),
            "path_evidence_config_id_unknown",
        )
        _require(self.fold_id in FOLD_IDS, "path_evidence_fold_id_unknown")
        _require(self.path_scenario in PATH_SCENARIOS, "path_evidence_scenario_unknown")
        _require_hex64(self.unique_evidence_hash, "path_evidence_unique_hash_malformed")
        _require_exact_int(
            self.unique_evidence_accepted_count,
            "path_evidence_unique_accepted_count_malformed",
        )
        _require_hex64(self.engine_input_hash, "path_evidence_engine_hash_malformed")
        _require_exact_int(
            self.engine_input_count, "path_evidence_engine_input_count_malformed"
        )
        for k, v in self.no_trade_reason_counts.items():
            _require(isinstance(k, str), "path_evidence_no_trade_reason_key_malformed")
            _require_exact_int(
                v, "path_evidence_no_trade_reason_value_malformed", min_value=1
            )
        _require(
            self.ledger_status
            in (
                "completed",
                "rejected",
                "crashed",
                "timeout",
                "not_selected",
                "never_selected",
            ),
            "path_evidence_ledger_status_unknown",
        )
        _require_exact_int(self.trade_count, "path_evidence_trade_count_malformed")
        _require_hex64(self.artifact_hash, "path_evidence_artifact_hash_malformed")


def cross_check_dual_evidence(
    unique: UniqueGeneratorEvidence,
    paths: dict[str, PathInvocationEvidence],
) -> None:
    """The one boundary proving every path traces back to the SAME unique
    accepted-input hash/count -- never a sum/intersection/first-path/
    tripled reconstruction."""
    _require(
        set(paths.keys()) == set(PATH_SCENARIOS),
        "dual_evidence_path_set_incomplete",
    )
    _require(
        unique.phase == "selected_oos",
        "dual_evidence_path_requires_selected_oos_unique_phase",
    )
    for name, path in paths.items():
        _require(path.path_scenario == name, "dual_evidence_path_scenario_key_mismatch")
        _require(
            path.strategy == unique.strategy
            and path.config_id == unique.config_id
            and path.fold_id == unique.fold_id,
            "dual_evidence_path_binding_mismatch",
        )
        _require(
            path.unique_evidence_hash == unique.accepted_input_hash,
            "dual_evidence_path_unique_hash_mismatch",
        )
        _require(
            path.unique_evidence_accepted_count == unique.accepted,
            "dual_evidence_path_unique_accepted_count_mismatch",
        )
        if unique.accepted > 0 and path.trade_count == 0:
            _require(
                sum(path.no_trade_reason_counts.values()) == unique.accepted,
                "dual_evidence_zero_trade_reason_subtotal_mismatch",
            )


@dataclass(frozen=True, slots=True)
class H4AttributionCrossCheck:
    ok: bool
    path_count: int
    trade_count: int
    raw_member_key_cross_seal: str

    def __post_init__(self) -> None:
        _require(type(self.ok) is bool and self.ok, "h4_attribution_cross_check_not_ok")
        _require_exact_int(self.path_count, "h4_attribution_path_count_malformed")
        _require_exact_int(self.trade_count, "h4_attribution_trade_count_malformed")
        _require(
            self.raw_member_key_cross_seal == FAKE_FREE_EMPIRICAL_CLOSURE,
            "h4_attribution_member_key_cross_seal_must_remain_deferred",
        )


def cross_check_h4_attribution_contract(
    *,
    h4_contract: H4AttributionContractResult,
    paths_by_key: dict[tuple[str, str, str], PathInvocationEvidence],
) -> H4AttributionCrossCheck:
    """Bind H4 raw paths to H5 invocation evidence without inventing keys.

    The engine input seal/count and terminal trade count are common existing
    authorities and can be checked now.  H6-A's member-trade-key recipe is not
    present in the merged predecessor, so raw-row-to-member-key cross-sealing
    remains the explicit H6-B integration responsibility.
    """
    _require(
        type(h4_contract) is H4AttributionContractResult
        and h4_contract.actual_h4_contract == "PASS"
        and h4_contract.envelope is not None,
        "h4_attribution_cross_check_requires_actual_pass",
    )
    _require(type(paths_by_key) is dict, "h4_attribution_paths_by_key_malformed")
    envelope = h4_contract.envelope
    expected_keys = {
        (path.lineage.row_id, path.lineage.fold_id, path.path_scenario)
        for path in envelope.paths
    }
    _require(
        expected_keys.issubset(paths_by_key),
        "h4_attribution_path_invocation_evidence_set_mismatch",
    )
    trade_count = 0
    for path in envelope.paths:
        key = (path.lineage.row_id, path.lineage.fold_id, path.path_scenario)
        evidence = paths_by_key.get(key)
        _require(
            type(evidence) is PathInvocationEvidence,
            "h4_attribution_path_invocation_evidence_missing",
        )
        _require(
            evidence.strategy == path.strategy
            and evidence.config_id == path.lineage.row_id
            and evidence.fold_id == path.lineage.fold_id
            and evidence.path_scenario == path.path_scenario,
            "h4_attribution_path_binding_mismatch",
        )
        _require(
            evidence.engine_input_hash == path.terminal.input_seal_sha256,
            "h4_attribution_engine_input_hash_mismatch",
        )
        _require(
            evidence.engine_input_count == path.engine_input_count,
            "h4_attribution_engine_input_count_mismatch",
        )
        _require(
            evidence.trade_count == len(path.rows),
            "h4_attribution_trade_count_mismatch",
        )
        _require(
            evidence.ledger_status == "completed",
            "h4_attribution_selected_path_not_completed",
        )
        trade_count += len(path.rows)
    return H4AttributionCrossCheck(
        ok=True,
        path_count=len(envelope.paths),
        trade_count=trade_count,
        raw_member_key_cross_seal=FAKE_FREE_EMPIRICAL_CLOSURE,
    )


@dataclass(frozen=True, slots=True)
class PboEvidence:
    strategy: str
    config_count: int
    day_count: int
    slices: int
    scenario_name: str
    value: float | None
    reason_codes: tuple[str, ...]
    source_hash: str
    input_hash: str
    artifact_hash: str

    def __post_init__(self) -> None:
        _require(self.strategy in STRATEGIES, "pbo_strategy_unknown")
        _require(self.config_count == PBO_CONFIG_COUNT, "pbo_config_count_not_24")
        _require(self.day_count == PBO_DAY_COUNT, "pbo_day_count_not_365")
        _require(self.slices == PBO_SLICES, "pbo_slices_not_4")
        _require(
            self.scenario_name == PBO_SCENARIO_NAME, "pbo_scenario_not_primary_stress17"
        )
        if self.value is not None:
            _require(type(self.value) is float, "pbo_value_malformed")
        _require(
            isinstance(self.reason_codes, tuple)
            and all(isinstance(r, str) for r in self.reason_codes),
            "pbo_reason_codes_malformed",
        )
        for h in (self.source_hash, self.input_hash, self.artifact_hash):
            _require_hex64(h, "pbo_hash_malformed")


def validate_pbo_evidence(pbo: PboEvidence | None) -> EnvelopeValidationResult:
    """Missing PBO (``None``) is a legitimate producer state, never a raise
    -- it makes the campaign structurally incomplete. An evaluator-failure
    reason code likewise makes it incomplete. A well-formed, present
    ``PboEvidence`` always passes here regardless of its ``value`` -- PBO can
    never flip a hard gate/verdict, so this function never inspects
    ``value`` for pass/fail purposes, only for presence/shape."""
    if pbo is None:
        return EnvelopeValidationResult(
            ok=False, incomplete_reasons=("missing_pbo_evidence",)
        )
    if pbo.reason_codes:
        return EnvelopeValidationResult(
            ok=False, incomplete_reasons=tuple(sorted(pbo.reason_codes))
        )
    return EnvelopeValidationResult(ok=True, incomplete_reasons=())
