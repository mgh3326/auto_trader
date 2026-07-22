"""Pure ROB-974 R3 production identity and non-production topology seam.

This module freezes the preregistered 3+9 rows over the real eight H4 folds.
It performs no corpus observation, persistence, network, broker, order, fill,
or H2 engine operation.  Production execution is deliberately fail-closed:
six preregistered S4 cells can accept observed ``|z| < 1`` candidates that the
frozen R2 H2 DTO refuses, so only a clearly named topology simulator is
available until a separately approved execution seam resolves that mismatch.
"""

from __future__ import annotations

import base64
import dataclasses
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Never

import rob974_h3_manifest as r2_manifest
import rob974_r3_identity as r3_identity
import rob974_r3_manifest as r3_manifest
from rob944_folds import Fold
from rob974_h4_contracts import SCENARIOS, exact_h4_folds
from rob974_h4_h6a_adapter import (
    PBO_SOURCE_FILES,
    RUNNER_SOURCE_FILES,
    build_production_h4_plan,
    source_bundle_sha256,
)
from rob974_r3_shape import (
    R3_CANONICAL_ROW_ORDER,
    Exact12MappingError,
    compute_exact_12_mapping_hash,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "R3_BLOCKED_CONFIG_IDS",
    "R3_OPERATIONAL_BLOCKER_REASON",
    "R3_OPERATIONAL_STATUS",
    "R3_PBO_SOURCE_FILES",
    "R3_RUNNER_SOURCE_FILES",
    "R3CandidateBuffer",
    "R3FreshEngine",
    "R3InvocationKey",
    "R3InvocationResult",
    "R3PlanError",
    "R3ProductionExecutionBlocked",
    "R3ProductionPlan",
    "R3SourcePins",
    "build_production_r3_plan",
    "run_r3_all_cell_campaign",
    "simulate_r3_all_cell_topology",
    "validate_r3_pbo_roster",
]

_R2_FROZEN_FULL_CAMPAIGN_HASH = (
    "2c47864c7ab661f16be6c414a1140944ec36832bb268e86183555b56c6f85f53"
)
_AUTHORITY_LABEL = "rob974-r3-preregistration-2026-07-22"
_PHASES: tuple[str, str] = ("train", "oos")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

R3_OPERATIONAL_STATUS = "INCOMPLETE"
R3_OPERATIONAL_BLOCKER_REASON = "h2_s4_observed_z_floor_blocks_preregistered_cells"
R3_BLOCKED_CONFIG_IDS: tuple[str, ...] = tuple(
    config.config_id
    for config in r3_manifest.FROZEN_R3_S4_CONFIGS
    if config.z_entry in (0.60, 0.80)
)
_EXPECTED_BLOCKED_CONFIG_IDS = tuple(f"S4-R3-{index:02d}" for index in range(3, 9))
if R3_BLOCKED_CONFIG_IDS != _EXPECTED_BLOCKED_CONFIG_IDS:
    raise RuntimeError("R3 execution blocker roster drifted")

Phase = Literal["train", "oos"]
R3Config = r3_manifest.R3S3Config | r3_manifest.R3S4Config
CandidateFactory = Callable[[Phase, R3Config, Fold], "R3CandidateBuffer"]
EngineFactory = Callable[["R3InvocationKey"], "R3FreshEngine"]


class R3PlanError(ValueError):
    """The R3 plan differs from its exact preregistered production identity."""


class R3ProductionExecutionBlocked(R3PlanError):
    """The current approved tree cannot execute every preregistered R3 cell."""


def _hex64(value: object, name: str) -> str:
    if (
        type(value) is not str
        or _HEX64_RE.fullmatch(value) is None
        or value == "0" * 64
    ):
        raise R3PlanError(f"{name} must be nonzero lowercase 64-hex")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _source_files(*logical_paths: str) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (logical_path, _REPO_ROOT / logical_path) for logical_path in logical_paths
    )


R3_RUNNER_SOURCE_FILES = RUNNER_SOURCE_FILES + _source_files(
    "research/nautilus_scalping/rob974_h3_gate_predicates.py",
    "research/nautilus_scalping/rob974_r3_shape.py",
    "research/nautilus_scalping/rob974_r3_identity.py",
    "research/nautilus_scalping/rob974_r3_accounting.py",
    "research/nautilus_scalping/rob974_r3_manifest.py",
    "research/nautilus_scalping/rob974_r3_h3_adapter.py",
    "research/nautilus_scalping/rob974_r3_gate_metrics.py",
    "research/nautilus_scalping/rob974_r3_gate_adapter.py",
    "research/nautilus_scalping/rob974_r3_relaxation.py",
    "research/nautilus_scalping/rob974_r3_relaxation_h2_adapter.py",
    "research/nautilus_scalping/rob974_r3_evidence_context.py",
    "research/nautilus_scalping/rob974_r3_plan.py",
    "research/nautilus_scalping/rob974_r3_postaudit.py",
    "app/services/rob974_r3_h6a_bridge.py",
    "app/services/rob974_r3_materializer.py",
)
R3_PBO_SOURCE_FILES = PBO_SOURCE_FILES + _source_files(
    "research/nautilus_scalping/rob974_r3_shape.py",
    "research/nautilus_scalping/rob974_r3_manifest.py",
    "research/nautilus_scalping/rob974_r3_plan.py",
)


@dataclass(frozen=True, slots=True)
class R3SourcePins:
    feature_source_sha256: str
    engine_source_sha256: str
    runner_source_sha256: str
    pbo_implementation_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "feature_source_sha256",
            "engine_source_sha256",
            "runner_source_sha256",
            "pbo_implementation_sha256",
        ):
            _hex64(getattr(self, name), name)

    def as_dict(self) -> dict[str, str]:
        return {
            "feature_source_sha256": self.feature_source_sha256,
            "engine_source_sha256": self.engine_source_sha256,
            "runner_source_sha256": self.runner_source_sha256,
            "pbo_implementation_sha256": self.pbo_implementation_sha256,
        }


def _fold_payload(fold: Fold) -> dict[str, int | str]:
    return {
        "fold_id": fold.fold_id,
        "fold_index": fold.fold_index,
        "train_start_ms": fold.train_start_ms,
        "train_end_ms": fold.train_end_ms,
        "embargo_start_ms": fold.embargo_start_ms,
        "embargo_end_ms": fold.embargo_end_ms,
        "oos_start_ms": fold.oos_start_ms,
        "oos_end_ms": fold.oos_end_ms,
    }


def _derive_campaign_run_id(full_campaign_hash: str) -> str:
    _hex64(full_campaign_hash, "full_campaign_hash")
    digest = canonical_sha256(
        {
            "full_campaign_hash": full_campaign_hash,
            "kind": "rob974_r3_h6a_primary_run",
        }
    )
    suffix = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip("=")
    return f"rob974r3-{suffix}"


def _row_payload(spec: r3_identity.R3RowSpec) -> dict[str, object]:
    return {
        "row_id": spec.row_id,
        "strategy_key": spec.strategy_key,
        "strategy_version": spec.strategy_version,
        "hypothesis": spec.hypothesis,
        "provenance": spec.provenance,
        "experiment_id": spec.experiment_id,
        "components": _plain(spec.components),
    }


def _plan_payload_without_run_identity(plan: R3ProductionPlan) -> dict[str, object]:
    return {
        "schema_version": "rob974.r3.production_plan.v1",
        "lineage": "ROB-974-R3",
        "production_state": "identity_ready_execution_incomplete",
        "r2_frozen_parent_full_campaign_hash": _R2_FROZEN_FULL_CAMPAIGN_HASH,
        "preregistration_document_sha256": (
            r3_manifest.PREREGISTRATION_DOCUMENT_SHA256
        ),
        "manifest_contract_version": r3_manifest.R3_MANIFEST_CONTRACT_VERSION,
        "manifest_contract_hash": r3_manifest.R3_MANIFEST_CONTRACT_HASH,
        "manifest": [dataclasses.asdict(row) for row in plan.manifest_rows],
        "rows": [_row_payload(spec) for spec in plan.row_specs],
        "ordered_mapping": [
            {"row_id": row_id, "experiment_id": experiment_id}
            for row_id, experiment_id in plan.ordered_mapping
        ],
        "exact_12_mapping_hash": plan.exact_12_mapping_hash,
        "folds": [_fold_payload(fold) for fold in plan.folds],
        "phases": list(plan.phases),
        "scenarios": list(plan.scenarios),
        "execution": {
            "selection": "none_all_preregistered_cells",
            "oos_threshold_feedback": False,
            "candidate_batches": len(plan.row_specs) * len(plan.folds) * 2,
            "engine_invocations": (
                len(plan.row_specs) * len(plan.folds) * 2 * len(plan.scenarios)
            ),
            "fresh_candidate_buffer_per_phase_fold_cell": True,
            "fresh_engine_state_per_phase_fold_cell_scenario": True,
            "operational_status": plan.operational_status,
            "production_execution_enabled": False,
            "blocker_reason": plan.operational_blocker_reason,
            "affected_config_ids": list(plan.blocked_config_ids),
            "frozen_h2_observed_z_abs_min": 1.0,
            "topology_simulator_is_non_production": True,
        },
        "pbo": {
            "S3": [row.config_id for row in r3_manifest.FROZEN_R3_S3_CONFIGS],
            "S4": [row.config_id for row in r3_manifest.FROZEN_R3_S4_CONFIGS],
            "family_counts": {"S3": 3, "S4": 9},
            "winner_selection_authority": False,
        },
        "source_pins": plan.source_pins.as_dict(),
    }


@dataclass(frozen=True, slots=True)
class R3ProductionPlan:
    manifest_rows: tuple[R3Config, ...]
    row_specs: tuple[r3_identity.R3RowSpec, ...]
    folds: tuple[Fold, ...]
    phases: tuple[str, str]
    scenarios: tuple[str, ...]
    source_pins: R3SourcePins
    ordered_mapping: tuple[tuple[str, str], ...]
    exact_12_mapping_hash: str
    operational_status: str
    operational_blocker_reason: str
    blocked_config_ids: tuple[str, ...]
    full_campaign_hash: str
    campaign_run_id: str

    def __post_init__(self) -> None:
        if self.manifest_rows is not r3_manifest.FROZEN_R3_ROSTER:
            raise R3PlanError("production R3 plan requires the issued manifest tuple")
        try:
            r3_manifest.validate_r3_manifest(self.manifest_rows)
        except (TypeError, ValueError) as exc:
            raise R3PlanError("production R3 manifest validation failed") from exc
        if type(self.row_specs) is not tuple or any(
            type(spec) is not r3_identity.R3RowSpec for spec in self.row_specs
        ):
            raise R3PlanError("row_specs must use an exact R3RowSpec tuple")
        if tuple(spec.row_id for spec in self.row_specs) != R3_CANONICAL_ROW_ORDER:
            raise R3PlanError("production R3 row-spec order drifted")
        for spec in self.row_specs:
            try:
                r3_identity.verify_r3_row_experiment_id(
                    spec, envelope_experiment_id=spec.experiment_id
                )
            except (TypeError, ValueError) as exc:
                raise R3PlanError(f"{spec.row_id}: experiment identity drift") from exc
        if self.folds != exact_h4_folds() or type(self.folds) is not tuple:
            raise R3PlanError("production R3 plan requires all eight real folds")
        if self.phases != _PHASES or type(self.phases) is not tuple:
            raise R3PlanError("production R3 phases must be exact TRAIN then OOS")
        if self.scenarios != SCENARIOS or type(self.scenarios) is not tuple:
            raise R3PlanError("production R3 scenario order drifted")
        if type(self.source_pins) is not R3SourcePins:
            raise R3PlanError("production R3 source pins use the wrong DTO")
        expected_mapping = tuple(
            (spec.row_id, spec.experiment_id) for spec in self.row_specs
        )
        if self.ordered_mapping != expected_mapping:
            raise R3PlanError("production R3 mapping differs from its row specs")
        try:
            mapping_hash = compute_exact_12_mapping_hash(self.ordered_mapping)
        except Exact12MappingError as exc:
            raise R3PlanError("production R3 exact-12 mapping is invalid") from exc
        if self.exact_12_mapping_hash != mapping_hash:
            raise R3PlanError("production R3 exact-12 mapping hash drifted")
        if self.operational_status != R3_OPERATIONAL_STATUS:
            raise R3PlanError("production R3 operational status must remain INCOMPLETE")
        if self.operational_blocker_reason != R3_OPERATIONAL_BLOCKER_REASON:
            raise R3PlanError("production R3 execution blocker reason drifted")
        if self.blocked_config_ids != R3_BLOCKED_CONFIG_IDS:
            raise R3PlanError("production R3 blocked-cell roster drifted")
        _hex64(self.full_campaign_hash, "full_campaign_hash")
        expected_full_hash = canonical_sha256(_plan_payload_without_run_identity(self))
        if self.full_campaign_hash != expected_full_hash:
            raise R3PlanError("production R3 full campaign hash drifted")
        if self.campaign_run_id != _derive_campaign_run_id(self.full_campaign_hash):
            raise R3PlanError("production R3 campaign run ID drifted")

    def to_payload(self) -> dict[str, object]:
        payload = _plan_payload_without_run_identity(self)
        return {
            **payload,
            "full_campaign_hash": self.full_campaign_hash,
            "campaign_run_id": self.campaign_run_id,
        }


def _strategy_contract_hash(
    contract: r3_manifest.R3StrategyContract,
) -> str:
    payload = {
        "family": contract.family,
        "key": contract.key,
        "version": contract.version,
        "preregistration_document_sha256": (
            r3_manifest.PREREGISTRATION_DOCUMENT_SHA256
        ),
        "manifest_contract_hash": r3_manifest.R3_MANIFEST_CONTRACT_HASH,
        "roster": [
            dataclasses.asdict(row)
            for row in r3_manifest.FROZEN_R3_ROSTER
            if row.config_id.startswith(f"{contract.family}-")
        ],
    }
    return canonical_sha256(payload)


def _contracts() -> dict[str, r3_identity.R3StrategyContractProvenance]:
    result: dict[str, r3_identity.R3StrategyContractProvenance] = {}
    for family, contract in (
        ("S3", r3_manifest.R3_S3_STRATEGY_CONTRACT),
        ("S4", r3_manifest.R3_S4_STRATEGY_CONTRACT),
    ):
        result[family] = r3_identity.R3StrategyContractProvenance(
            strategy_slug=family,
            strategy_key=contract.key,
            strategy_version=contract.version,
            contract_hash=_strategy_contract_hash(contract),
            contract_key=contract.key,
            provenance="production",
            expected_contract_hash=contract.contract_hash,
        )
    return result


def _rows() -> tuple[r3_identity.R3CampaignConfigRow, ...]:
    hypotheses = {
        "S3": r2_manifest.S3_HYPOTHESIS_UTF8.decode("utf-8"),
        "S4": r2_manifest.S4_HYPOTHESIS_UTF8.decode("utf-8"),
    }
    rows: list[r3_identity.R3CampaignConfigRow] = []
    for config in r3_manifest.FROZEN_R3_ROSTER:
        params = dataclasses.asdict(config)
        del params["config_id"]
        family = config.config_id[:2]
        rows.append(
            r3_identity.R3CampaignConfigRow(
                row_id=config.config_id,
                params=params,
                hypothesis=hypotheses[family],
                authority_label=_AUTHORITY_LABEL,
                provenance="production",
            )
        )
    return tuple(rows)


def _source_pins(r2_plan: Any) -> R3SourcePins:
    return R3SourcePins(
        feature_source_sha256=r2_plan.source_pins.feature_source_sha256,
        engine_source_sha256=r2_plan.source_pins.engine_source_sha256,
        runner_source_sha256=source_bundle_sha256(R3_RUNNER_SOURCE_FILES),
        pbo_implementation_sha256=source_bundle_sha256(R3_PBO_SOURCE_FILES),
    )


def _anchor_specs(r2_plan: Any) -> dict[str, Any]:
    by_id = {spec.row_id: spec for spec in r2_plan.row_specs}
    try:
        return {"S3": by_id["S3-03"], "S4": by_id["S4-02"]}
    except KeyError as exc:
        raise R3PlanError("frozen R2 anchor row is unavailable") from exc


def _identity_components(
    anchors: dict[str, Any], source_pins: R3SourcePins
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    shared = {
        name: _plain(anchors["S3"].components[name])
        for name in ("dataset_manifest", "universe", "benchmark", "mdd")
    }
    for name in shared:
        if shared[name] != _plain(anchors["S4"].components[name]):
            raise R3PlanError(f"frozen R2 shared component {name} drifted")

    pit: dict[str, dict[str, Any]] = {}
    frozen: dict[str, dict[str, Any]] = {}
    policy: dict[str, dict[str, Any]] = {}
    cost: dict[str, dict[str, Any]] = {}
    for family, count in (("S3", 3), ("S4", 9)):
        anchor = anchors[family]
        anchor_manifest = (
            r3_manifest.S3_R2_ANCHOR if family == "S3" else r3_manifest.S4_R2_ANCHOR
        )
        contract = (
            r3_manifest.R3_S3_STRATEGY_CONTRACT
            if family == "S3"
            else r3_manifest.R3_S4_STRATEGY_CONTRACT
        )
        roster = (
            r3_manifest.FROZEN_R3_S3_CONFIGS
            if family == "S3"
            else r3_manifest.FROZEN_R3_S4_CONFIGS
        )

        pit[family] = _plain(anchor.components["pit"])
        pit[family].update(
            {
                "phases": list(_PHASES),
                "oos_scope": "all_preregistered_cells",
                "oos_threshold_feedback": False,
            }
        )
        frozen[family] = {
            "preregistration_document_sha256": (
                r3_manifest.PREREGISTRATION_DOCUMENT_SHA256
            ),
            "manifest_contract_version": r3_manifest.R3_MANIFEST_CONTRACT_VERSION,
            "manifest_contract_hash": r3_manifest.R3_MANIFEST_CONTRACT_HASH,
            "strategy_contract": dataclasses.asdict(contract),
            "r2_anchor": dataclasses.asdict(anchor_manifest),
            "moving_axes": (
                ["S_min", "M_min_bp"] if family == "S3" else ["z_entry", "d_min_bp"]
            ),
            "source_pins": source_pins.as_dict(),
        }
        inherited_policy = _plain(anchor.components["policy"])
        inherited_policy.pop("train_selection", None)
        inherited_policy.pop("selected_oos", None)
        inherited_policy.update(
            {
                "all_cell_execution": {
                    "configs": count,
                    "config_ids": [config.config_id for config in roster],
                    "phases": list(_PHASES),
                    "scenarios": list(SCENARIOS),
                    "winner_selection": "none",
                    "oos_threshold_feedback": False,
                    "fresh_candidate_buffer": True,
                    "fresh_engine_per_scenario": True,
                },
                "operational_execution_seam": {
                    "status": R3_OPERATIONAL_STATUS,
                    "reason": R3_OPERATIONAL_BLOCKER_REASON,
                    "affected_config_ids": list(R3_BLOCKED_CONFIG_IDS),
                },
                "pbo_exact_family_roster": [config.config_id for config in roster],
            }
        )
        if family == "S4":
            inherited_policy["historical_pair_execution"] = {
                "pair_executor_validated": False,
                "pair_exec_fail": "not_evaluated",
                "promotion": "blocked_pending_pair_executor",
            }
        policy[family] = inherited_policy

        cost[family] = _plain(anchor.components["cost"])
        pbo = cost[family].get("pbo")
        if type(pbo) is not dict:
            raise R3PlanError(f"frozen R2 {family} PBO cost component drifted")
        pbo.update(
            {
                "configs_per_strategy": count,
                "config_ids": [config.config_id for config in roster],
            }
        )
    return shared, pit, frozen, policy, cost


def build_production_r3_plan() -> R3ProductionPlan:
    """Build the real exact-12 identity while keeping execution fail-closed."""

    r2_plan = build_production_h4_plan()
    if r2_plan.full_campaign_hash != _R2_FROZEN_FULL_CAMPAIGN_HASH:
        raise R3PlanError("frozen R2 production full campaign hash drifted")
    source_pins = _source_pins(r2_plan)
    shared, pit, frozen, policy, cost = _identity_components(
        _anchor_specs(r2_plan), source_pins
    )
    row_specs = r3_identity.build_r3_campaign_row_specs(
        _rows(),
        contracts=_contracts(),
        shared_components=shared,
        pit_component_by_slug=pit,
        frozen_config_component_by_slug=frozen,
        policy_component_by_slug=policy,
        cost_component_by_slug=cost,
    )
    mapping = tuple((spec.row_id, spec.experiment_id) for spec in row_specs)
    mapping_hash = compute_exact_12_mapping_hash(mapping)
    provisional = R3ProductionPlan.__new__(R3ProductionPlan)
    values = {
        "manifest_rows": r3_manifest.FROZEN_R3_ROSTER,
        "row_specs": row_specs,
        "folds": exact_h4_folds(),
        "phases": _PHASES,
        "scenarios": SCENARIOS,
        "source_pins": source_pins,
        "ordered_mapping": mapping,
        "exact_12_mapping_hash": mapping_hash,
        "operational_status": R3_OPERATIONAL_STATUS,
        "operational_blocker_reason": R3_OPERATIONAL_BLOCKER_REASON,
        "blocked_config_ids": R3_BLOCKED_CONFIG_IDS,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    full_campaign_hash = canonical_sha256(
        _plan_payload_without_run_identity(provisional)
    )
    return R3ProductionPlan(
        **values,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=_derive_campaign_run_id(full_campaign_hash),
    )


def validate_r3_pbo_roster(
    family: str, roster: object
) -> tuple[r3_manifest.R3S3Config, ...] | tuple[r3_manifest.R3S4Config, ...]:
    if type(family) is not str or family not in ("S3", "S4"):
        raise R3PlanError("PBO family must be exact S3 or S4")
    expected = (
        r3_manifest.FROZEN_R3_S3_CONFIGS
        if family == "S3"
        else r3_manifest.FROZEN_R3_S4_CONFIGS
    )
    if roster is not expected:
        raise R3PlanError("PBO requires the issued exact ordered R3 family roster")
    return expected


@dataclass(frozen=True, slots=True)
class R3CandidateBuffer:
    phase: Phase
    row_id: str
    fold_id: str
    candidates: tuple[object, ...]

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise R3PlanError("candidate-buffer phase must be train or oos")
        if self.row_id not in R3_CANONICAL_ROW_ORDER:
            raise R3PlanError("candidate-buffer row is outside exact-12")
        if self.fold_id not in tuple(f"fold-{index:02d}" for index in range(8)):
            raise R3PlanError("candidate-buffer fold is outside exact-eight")
        if type(self.candidates) is not tuple:
            raise R3PlanError("candidate buffer must be an immutable exact tuple")


@dataclass(frozen=True, slots=True)
class R3InvocationKey:
    phase: Phase
    row_id: str
    fold_id: str
    scenario: str

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise R3PlanError("invocation phase must be train or oos")
        if self.row_id not in R3_CANONICAL_ROW_ORDER:
            raise R3PlanError("invocation row is outside exact-12")
        if self.fold_id not in tuple(f"fold-{index:02d}" for index in range(8)):
            raise R3PlanError("invocation fold is outside exact-eight")
        if self.scenario not in SCENARIOS:
            raise R3PlanError("invocation scenario is outside exact-three")


@dataclass(frozen=True, slots=True)
class R3FreshEngine:
    invocation: R3InvocationKey
    state_token: object
    execute: Callable[[tuple[object, ...]], object]

    def __post_init__(self) -> None:
        if type(self.invocation) is not R3InvocationKey:
            raise R3PlanError("fresh engine requires an exact invocation key")
        if self.state_token is None:
            raise R3PlanError("fresh engine requires a non-None state token")
        if not callable(self.execute):
            raise R3PlanError("fresh engine execute seam must be callable")


@dataclass(frozen=True, slots=True)
class R3InvocationResult:
    invocation: R3InvocationKey
    outcome: object

    def __post_init__(self) -> None:
        if type(self.invocation) is not R3InvocationKey:
            raise R3PlanError("result requires an exact invocation key")


def _require_plan(plan: object) -> R3ProductionPlan:
    if type(plan) is not R3ProductionPlan:
        raise R3PlanError("R3 operation requires an exact production plan")
    return plan


def run_r3_all_cell_campaign(
    plan: R3ProductionPlan,
    *,
    candidate_factory: CandidateFactory,
    engine_factory: EngineFactory,
) -> Never:
    """Refuse production globally before the first callback while incomplete."""

    checked = _require_plan(plan)
    raise R3ProductionExecutionBlocked(
        f"{checked.operational_status}: {checked.operational_blocker_reason}; "
        "zero candidate or engine callbacks were invoked"
    )


def simulate_r3_all_cell_topology(
    plan: R3ProductionPlan,
    *,
    candidate_factory: CandidateFactory,
    engine_factory: EngineFactory,
) -> tuple[R3InvocationResult, ...]:
    """Exercise only the closed fan-out topology; this is not a campaign run."""

    checked = _require_plan(plan)
    if not callable(candidate_factory) or not callable(engine_factory):
        raise R3PlanError("topology simulator factories must be callable")
    candidate_buffers: list[R3CandidateBuffer] = []
    engine_handles: list[R3FreshEngine] = []
    engine_tokens: list[object] = []
    results: list[R3InvocationResult] = []
    for phase_value in checked.phases:
        phase: Phase = phase_value  # type: ignore[assignment]
        for fold in checked.folds:
            for config in checked.manifest_rows:
                batch = candidate_factory(phase, config, fold)
                if type(batch) is not R3CandidateBuffer:
                    raise R3PlanError("candidate factory returned the wrong DTO")
                if (
                    batch.phase != phase
                    or batch.row_id != config.config_id
                    or batch.fold_id != fold.fold_id
                ):
                    raise R3PlanError("candidate buffer identity differs from its cell")
                if any(batch is previous for previous in candidate_buffers):
                    raise R3PlanError("candidate buffer was reused across cells")
                candidate_buffers.append(batch)
                for scenario in checked.scenarios:
                    key = R3InvocationKey(
                        phase=phase,
                        row_id=config.config_id,
                        fold_id=fold.fold_id,
                        scenario=scenario,
                    )
                    engine = engine_factory(key)
                    if type(engine) is not R3FreshEngine:
                        raise R3PlanError("engine factory returned the wrong DTO")
                    if engine.invocation != key:
                        raise R3PlanError(
                            "fresh engine identity differs from invocation"
                        )
                    if any(engine is previous for previous in engine_handles):
                        raise R3PlanError("engine handle was reused across invocations")
                    if any(engine.state_token is token for token in engine_tokens):
                        raise R3PlanError("engine state was reused across invocations")
                    engine_handles.append(engine)
                    engine_tokens.append(engine.state_token)
                    results.append(
                        R3InvocationResult(
                            invocation=key,
                            outcome=engine.execute(batch.candidates),
                        )
                    )
    return tuple(results)
