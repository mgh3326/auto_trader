"""ROB-982 CP9 -- real H4 source pins and H6-A production-plan closure.

This is the single pure bridge from H4 to the merged H6-A identity kernel.  It
recomputes four closed source-bundle seals from the current files' raw bytes,
asks H6-A's reviewed H2/H3 adapter for the real 48 production rows, supplies
H4-owned policy/components, and builds one ``mode="production_plan"``
envelope.  Callers cannot inject source pins or production rows.

The full-corpus fake-free run remains an H6-B integration-E2E responsibility;
this module performs no corpus-row load, DB/network/process/broker operation,
environment access, randomness, or current-time read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import rob974_h3_manifest as h3_manifest
import rob974_h6a_h2h3_adapter as h6a_adapter
import rob974_h6a_identity as h6a_identity
import rob974_h6a_payload as h6a_payload
from rob940_cost_model import (
    COST_SCENARIO_BASE,
    COST_SCENARIO_PRIMARY_STRESS,
    COST_SCENARIO_UPWARD_STRESS,
)
from rob974_h4_contracts import (
    PBO_DAYS,
    PBO_SCENARIO,
    PBO_SLICES,
    SCENARIOS,
    WINDOW_END_MS,
    WINDOW_START_MS,
    H4SourcePins,
    attribution_contract,
    campaign_verdict_contract,
    exact_h4_folds,
    scorecard_contract,
)
from rob974_lineage import (
    PARENT_CONTENT_SHA256,
    PARENT_MANIFEST_SHA256,
    SELECTED_UNIVERSE,
    verify_parent,
)

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "ENGINE_SOURCE_FILES",
    "FEATURE_SOURCE_FILES",
    "PBO_SOURCE_FILES",
    "RUNNER_SOURCE_FILES",
    "ContractDriftError",
    "ProductionH4Plan",
    "SourcePinError",
    "build_production_h4_plan",
    "build_production_source_pins",
    "source_bundle_sha256",
]

ContractDriftError = h6a_adapter.ContractDriftError

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent


class SourcePinError(ValueError):
    """A closed source inventory cannot be sealed from exact raw bytes."""


def _source_files(*logical_paths: str) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (logical_path, _REPO_ROOT / logical_path) for logical_path in logical_paths
    )


# H1: selected-universe feature plane and its verified parent-lineage boundary.
FEATURE_SOURCE_FILES = _source_files(
    "research/nautilus_scalping/rob941_manifest.py",
    "research/nautilus_scalping/rob974_features.py",
    "research/nautilus_scalping/rob974_lineage.py",
)

# H2: actual engines plus every execution-affecting ingress/cost/funding DTO seam.
ENGINE_SOURCE_FILES = _source_files(
    "research/nautilus_scalping/rob940_cost_model.py",
    "research/nautilus_scalping/rob941_funding_sidecar.py",
    "research/nautilus_scalping/rob974_h2_dtos.py",
    "research/nautilus_scalping/rob974_h2_h1_bridge.py",
    "research/nautilus_scalping/rob974_h2_ingress.py",
    "research/nautilus_scalping/rob974_h2_s3_engine.py",
    "research/nautilus_scalping/rob974_h2_s4_engine.py",
    "research/nautilus_scalping/rob974_h2_scenarios.py",
)

# H3 generation + H4 phase/selection/terminal orchestration and this identity bridge.
RUNNER_SOURCE_FILES = _source_files(
    "research_contracts/canonical_hash.py",
    "research/nautilus_scalping/rob944_diagnostic_evidence.py",
    "research/nautilus_scalping/rob944_folds.py",
    "research/nautilus_scalping/rob974_h3_evidence.py",
    "research/nautilus_scalping/rob974_h3_h2_adapter.py",
    "research/nautilus_scalping/rob974_h3_manifest.py",
    "research/nautilus_scalping/rob974_h3_s3.py",
    "research/nautilus_scalping/rob974_h3_s4.py",
    "research/nautilus_scalping/rob974_h4_adapter.py",
    "research/nautilus_scalping/rob974_h4_contracts.py",
    "research/nautilus_scalping/rob974_h4_h6a_adapter.py",
    "research/nautilus_scalping/rob974_h4_plan.py",
    "research/nautilus_scalping/rob974_h4_runner.py",
    "research/nautilus_scalping/rob974_h4_selection.py",
)

# PBO's own aggregator plus the frozen day-grid and reused CSCV primitive.
PBO_SOURCE_FILES = _source_files(
    "research/nautilus_scalping/rob945_pbo_grid.py",
    "research/nautilus_scalping/rob974_h4_pbo.py",
    "research_contracts/honest_offline_gate.py",
)


def source_bundle_sha256(files: object) -> str:
    """Seal an ordered logical-file inventory from exact raw bytes.

    Logical paths are included alongside each raw-byte SHA-256, avoiding
    concatenation ambiguity and making a rename/order change identity-visible.
    """
    if type(files) is not tuple or not files:
        raise SourcePinError("source bundle must be a non-empty built-in tuple")

    seen_logical: set[str] = set()
    seen_physical: set[Path] = set()
    rows: list[dict[str, str]] = []
    for item in files:
        if type(item) is not tuple or len(item) != 2:
            raise SourcePinError(
                "source entry must be an exact (logical_path, Path) tuple"
            )
        logical_path, path = item
        if type(logical_path) is not str or not logical_path:
            raise SourcePinError("logical source path must be a non-empty built-in str")
        logical = PurePosixPath(logical_path)
        if (
            logical.is_absolute()
            or ".." in logical.parts
            or str(logical) != logical_path
        ):
            raise SourcePinError(
                "logical source path must be normalized and repo-relative"
            )
        if logical_path in seen_logical:
            raise SourcePinError(f"duplicate logical source path: {logical_path}")
        if not isinstance(path, Path):
            raise SourcePinError("physical source path must be pathlib.Path")
        resolved = path.resolve()
        if resolved in seen_physical:
            raise SourcePinError(f"duplicate physical source path: {resolved}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SourcePinError(
                f"cannot read source bytes for {logical_path}"
            ) from exc
        if not path.is_file():
            raise SourcePinError(f"source path is not a regular file: {logical_path}")
        seen_logical.add(logical_path)
        seen_physical.add(resolved)
        rows.append(
            {
                "logical_path": logical_path,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    return canonical_sha256(
        {"schema_version": "rob974.source_bundle.v1", "files": rows}
    )


def build_production_source_pins() -> h6a_payload.RequiredSourcePins:
    """Recompute all required pins; no caller-supplied digest is accepted."""
    pins = h6a_payload.RequiredSourcePins(
        feature_source_sha256=source_bundle_sha256(FEATURE_SOURCE_FILES),
        engine_source_sha256=source_bundle_sha256(ENGINE_SOURCE_FILES),
        runner_source_sha256=source_bundle_sha256(RUNNER_SOURCE_FILES),
        pbo_implementation_sha256=source_bundle_sha256(PBO_SOURCE_FILES),
    )
    pins.require_production_ready()
    return pins


def _fold_payload() -> tuple[dict[str, int | str], ...]:
    return tuple(
        {
            "fold_id": fold.fold_id,
            "fold_index": fold.fold_index,
            "train_start_ms": fold.train_start_ms,
            "train_end_ms": fold.train_end_ms,
            "embargo_start_ms": fold.embargo_start_ms,
            "embargo_end_ms": fold.embargo_end_ms,
            "oos_start_ms": fold.oos_start_ms,
            "oos_end_ms": fold.oos_end_ms,
        }
        for fold in exact_h4_folds()
    )


def _path_membership() -> dict[str, dict[str, object]]:
    scenarios = (
        ("base13", COST_SCENARIO_BASE),
        ("primary_stress17", COST_SCENARIO_PRIMARY_STRESS),
        ("upward_stress22", COST_SCENARIO_UPWARD_STRESS),
    )
    if tuple(name for name, _ in scenarios) != SCENARIOS:
        raise ContractDriftError("CONTRACT_DRIFT: H4/H2 scenario order differs")
    return {
        name: {
            "round_trip_all_in_bps": scenario.all_in_bps,
            "fresh_engine_state": True,
            "global_path": True,
            "linear_revaluation_forbidden": True,
        }
        for name, scenario in scenarios
    }


def _funding_policy() -> dict[str, object]:
    return {
        "gate_order": "h3_global_arbitration_then_exact_entry_then_funding_then_h2_open",
        "strict_expected_debit_limit_bps": 3.0,
        "exact_limit_passes": True,
        "credits_pass": True,
        "s3_missing_reason": "funding_evidence_unavailable",
        "s4_requires_both_legs": True,
        "realized_interval": "[entry,exit)",
        "realized_weighting": "one_time_entry_frozen_basket_weighted",
        "scenario_cost_double_charge_forbidden": True,
    }


def _pbo_contract() -> dict[str, object]:
    return {
        "window_start_ms": WINDOW_START_MS,
        "window_end_ms": WINDOW_END_MS,
        "path_scenario": PBO_SCENARIO,
        "configs_per_strategy": 24,
        "days_per_config": PBO_DAYS,
        "slices": PBO_SLICES,
        "reference_only": True,
    }


def _campaign_policy() -> h6a_payload.CampaignPolicy:
    return h6a_payload.CampaignPolicy(
        folds=_fold_payload(),
        embargo_hours=3,
        horizons={
            "phase_interval": "[start,end)",
            "exact_equality_at_phase_end": "accepted",
            "one_ms_overrun": "rejected",
            "S3": {
                "max_hold_4h_bars": 12,
                "reason_by_phase": {
                    "train": "insufficient_train_exit_horizon",
                    "selected_oos": "insufficient_oos_exit_horizon",
                    "pbo": "insufficient_pbo_exit_horizon",
                },
            },
            "S4": {
                "max_hold_4h_bars": 9,
                "reason_by_phase": {
                    "train": "insufficient_train_exit_horizon",
                    "selected_oos": "insufficient_oos_exit_horizon",
                    "pbo": "insufficient_pbo_exit_horizon",
                },
            },
        },
        selection_authority=(
            "train_only_common_config_eligible_unit_equal_weight_E17_desc_"
            "then_PF_desc_then_config_id_asc"
        ),
        path_membership=_path_membership(),
        funding_policy=_funding_policy(),
        gates_bins={
            "scorecard": scorecard_contract(),
            "verdict": campaign_verdict_contract(),
            "unique_generator_schema": "rob974-h3-generator-evidence-v1",
            "invocation_schema": "rob974-h4-invocation-evidence-v1",
            "strategy_diagnostic_bins": {
                slug: h3_manifest.strategy_contract_payload(slug)["diagnostic_bins"]
                for slug in ("S3", "S4")
            },
        },
        pbo_contract=_pbo_contract(),
        pair_order=h3_manifest.PAIRS,
        s4_tri_state_policy=(
            "historical_only_pair_executor_not_evaluated_demo_ineligible"
        ),
    )


def _verified_parent_corpus() -> dict[str, object]:
    manifest = verify_parent()
    if manifest.content_hash() != PARENT_CONTENT_SHA256:
        raise ContractDriftError("CONTRACT_DRIFT: verified parent content hash drift")
    return {
        "schema_version": "rob974.h4.parent_corpus.v1",
        "content_sha256": PARENT_CONTENT_SHA256,
        "physical_manifest_sha256": PARENT_MANIFEST_SHA256,
        "window_start_ms": WINDOW_START_MS,
        "window_end_ms": WINDOW_END_MS,
        "selected_universe": list(SELECTED_UNIVERSE),
        "manifest": manifest.to_dict(),
    }


def _shared_components(parent_corpus: dict[str, object]) -> dict[str, object]:
    return {
        "dataset_manifest": parent_corpus,
        "universe": {
            "symbols": list(h3_manifest.SYMBOLS),
            "pairs": list(h3_manifest.PAIRS),
            "window": [WINDOW_START_MS, WINDOW_END_MS],
            "per_symbol_or_pair_parameter_override": "forbidden",
        },
        "benchmark": {
            "kind": "none_explicit_sentinel",
            "selection_or_gate_authority": False,
        },
        "mdd": {
            "path_scope": "account_global_for_each_strategy",
            "source": "actual_h2_terminal_trade_stream",
            "downstream_scorecard_only": True,
        },
    }


def _pit_components() -> dict[str, dict[str, object]]:
    folds = list(_fold_payload())
    return {
        slug: {
            "folds": folds,
            "stateless_raw_past_recompute": True,
            "mutable_state_carry": "forbidden",
            "emit_select_enter_trade_score": "exact_phase_only",
            "entry": "exact_contiguous_1m_open_at_decision_close_ts",
            "later_tick_scan": "forbidden",
            "max_hold_4h_bars": 12 if slug == "S3" else 9,
            "funding": _funding_policy(),
        }
        for slug in ("S3", "S4")
    }


def _frozen_config_components() -> dict[str, dict[str, object]]:
    return {
        slug: {
            "strategy_contract": h3_manifest.strategy_contract_payload(slug),
            "strategy_contract_hash": (
                h3_manifest.S3_STRATEGY_CONTRACT.contract_hash
                if slug == "S3"
                else h3_manifest.S4_STRATEGY_CONTRACT.contract_hash
            ),
        }
        for slug in ("S3", "S4")
    }


def _policy_components() -> dict[str, dict[str, object]]:
    scorecard = scorecard_contract()
    attribution = attribution_contract()
    return {
        slug: {
            "train_selection": {
                "configs": 24,
                "unit": "symbol" if slug == "S3" else "pair",
                "completed_trades_per_eligible_unit_min": 5,
                "eligible_units_min": 2,
                "common_winner": True,
                "oos_or_bin_leakage": "forbidden",
                "rank": ["equal_weight_unit_E17_desc", "PF_desc", "config_id_asc"],
            },
            "selected_oos": {
                "generator_calls_per_selected_fold": 1,
                "fresh_engine_calls": list(SCENARIOS),
                "non_selected_reason": "not_selected",
                "aggregate_never_selected_reason": "never_selected",
            },
            "evidence": {
                "unique_before_horizon_funding_engine": True,
                "dual_surface": ["unique_generator_evidence", "invocation_evidence"],
                "diagnostics_excluded_from_semantic_hashes": True,
            },
            "scorecard": {"common": scorecard["common"], slug: scorecard[slug]},
            "selected_oos_attribution": {
                "common": {
                    "schema_version": attribution["schema_version"],
                    "contract_provenance": attribution["contract_provenance"],
                    "market_return": attribution["market_return"],
                    "tercile": attribution["tercile"],
                    "realized_holding_minutes": attribution["realized_holding_minutes"],
                },
                slug: attribution[slug],
            },
            "verdict": campaign_verdict_contract(),
        }
        for slug in ("S3", "S4")
    }


def _cost_components() -> dict[str, dict[str, object]]:
    return {
        slug: {
            "paths": _path_membership(),
            "E0": "mean_price_only_gross_bps_on_primary_stress_membership",
            "funding": _funding_policy(),
            "pbo": _pbo_contract(),
        }
        for slug in ("S3", "S4")
    }


@dataclass(frozen=True, slots=True)
class ProductionH4Plan:
    row_specs: tuple[h6a_identity.H6ARowSpec, ...]
    envelope: h6a_payload.H6ACampaignEnvelope
    source_pins: h6a_payload.RequiredSourcePins
    h4_source_pins: H4SourcePins
    full_campaign_hash: str
    campaign_run_id: str
    expected_attempt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        h6a_identity.assert_specs_in_canonical_order(self.row_specs)
        if self.expected_attempt_ids != h6a_identity.CANONICAL_ROW_ORDER:
            raise ValueError("production H4 plan must expose exact canonical 48 IDs")
        if self.envelope.mode != "production_plan":
            raise ValueError("production H4 plan requires H6-A production_plan mode")
        if self.envelope.row_specs != self.row_specs:
            raise ValueError("production H4 row specs differ from envelope rows")
        if self.envelope.source_pins != self.source_pins:
            raise ValueError("production H4 source pins differ from envelope pins")
        if self.full_campaign_hash != self.envelope.full_campaign_hash():
            raise ValueError("production H4 full campaign hash drift")
        h6a_payload.verify_primary_run_id(
            self.campaign_run_id, full_campaign_hash=self.full_campaign_hash
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rob974.h4.production_plan.v1",
            "production_state": "h4_h6a_identity_ready_h6b_e2e_deferred",
            "full_campaign_hash": self.full_campaign_hash,
            "campaign_run_id": self.campaign_run_id,
            "expected_attempt_ids": list(self.expected_attempt_ids),
            "h4_source_pins": {
                "runner_bundle_sha256": self.h4_source_pins.runner_bundle_sha256,
                "pbo_source_sha256": self.h4_source_pins.pbo_source_sha256,
            },
            "envelope": self.envelope.to_dict(),
        }


def build_production_h4_plan() -> ProductionH4Plan:
    """Build the deterministic real H4/H6-A production identity plan."""
    h6a_adapter.verify_h2h3_contract()
    source_pins = build_production_source_pins()
    h4_source_pins = H4SourcePins(
        runner_bundle_sha256=source_pins.runner_source_sha256,
        pbo_source_sha256=source_pins.pbo_implementation_sha256,
    )
    parent_corpus = _verified_parent_corpus()
    row_specs = h6a_adapter.build_production_campaign_row_specs(
        shared_components=_shared_components(parent_corpus),
        pit_component_by_slug=_pit_components(),
        frozen_config_component_by_slug=_frozen_config_components(),
        policy_component_by_slug=_policy_components(),
        cost_component_by_slug=_cost_components(),
    )
    envelope = h6a_payload.build_campaign_envelope(
        row_specs=row_specs,
        parent_corpus=parent_corpus,
        campaign_policy=_campaign_policy(),
        source_pins=source_pins,
        mode="production_plan",
    )
    full_campaign_hash = envelope.full_campaign_hash()
    campaign_run_id = h6a_payload.derive_primary_run_id(full_campaign_hash)
    return ProductionH4Plan(
        row_specs=row_specs,
        envelope=envelope,
        source_pins=source_pins,
        h4_source_pins=h4_source_pins,
        full_campaign_hash=full_campaign_hash,
        campaign_run_id=campaign_run_id,
        expected_attempt_ids=h6a_identity.CANONICAL_ROW_ORDER,
    )
