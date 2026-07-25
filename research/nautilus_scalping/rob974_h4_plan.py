"""ROB-982 CP1 deterministic, non-production H4 campaign-plan builder.

The fixture builder is intentionally a plan only.  It does not inspect the
filesystem or corpus and has no run-id argument; the canonical hash is derived
solely from the closed semantic payload below.
"""

from __future__ import annotations

from dataclasses import dataclass

from rob974_h3_manifest import (
    FROZEN_S3_CONFIGS,
    FROZEN_S4_CONFIGS,
    PAIRS,
    RESEARCH_DOCUMENT_SHA256,
    S3_STRATEGY_CONTRACT,
    S4_STRATEGY_CONTRACT,
    SYMBOLS,
)
from rob974_h4_contracts import (
    PBO_DAYS,
    PBO_SCENARIO,
    PBO_SLICES,
    SCENARIOS,
    WINDOW_END_MS,
    WINDOW_START_MS,
    H4SourcePins,
    campaign_verdict_contract,
    exact_h4_folds,
    scorecard_contract,
    validate_exact_config_ids,
)

from research_contracts.canonical_hash import canonical_sha256


def _fold_payload() -> list[dict[str, object]]:
    return [
        {
            "fold_id": fold.fold_id,
            "train": [fold.train_start_ms, fold.train_end_ms],
            "embargo": [fold.embargo_start_ms, fold.embargo_end_ms],
            "oos": [fold.oos_start_ms, fold.oos_end_ms],
        }
        for fold in exact_h4_folds()
    ]


def _strategy_payload(strategy: str) -> dict[str, object]:
    configs = FROZEN_S3_CONFIGS if strategy == "S3" else FROZEN_S4_CONFIGS
    config_ids = tuple(config.config_id for config in configs)
    validate_exact_config_ids(strategy, config_ids)
    contract = S3_STRATEGY_CONTRACT if strategy == "S3" else S4_STRATEGY_CONTRACT
    return {
        "strategy": strategy,
        "config_ids": list(config_ids),
        "strategy_contract_key": contract.key,
        "strategy_contract_version": contract.version,
        "strategy_contract_hash": contract.contract_hash,
        "selection": {
            "phase": "train_only",
            "scenario": "primary_stress17",
            "unit": "symbol" if strategy == "S3" else "pair",
            "minimum_completed_train_basket_trades": 5,
            "minimum_eligible_units": 2,
            "rank": [
                "eligible_unit_equal_weight_mean_E17_desc",
                "PF_desc",
                "config_id_asc",
            ],
            "common_winner_all_units": True,
            "pooled_expectancy": "report_only",
        },
    }


@dataclass(frozen=True, slots=True)
class H4FixturePlan:
    payload: dict[str, object]
    plan_hash: str
    expected_attempt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.payload) is not dict:
            raise TypeError("payload must be a built-in dict")
        if type(self.plan_hash) is not str or len(self.plan_hash) != 64:
            raise TypeError("plan_hash must be a lowercase SHA-256")
        expected = tuple(f"S3-{i:02d}" for i in range(24)) + tuple(
            f"S4-{i:02d}" for i in range(24)
        )
        if self.expected_attempt_ids != expected:
            raise ValueError("H4 requires the exact ordered 48 logical attempts")
        if self.plan_hash != canonical_sha256(self.payload):
            raise ValueError("fixture plan hash does not seal its payload")


def build_fixture_plan(*, source_pins: object) -> H4FixturePlan:
    """Build the deterministic CP1 fixture payload without any side effect.

    ``source_pins`` is an exact typed audit boundary, not a run identity.  It
    is supplied by the later source-audit adapter and is sealed here only after
    rejecting placeholders; CP9 replaces fixture mode with independently
    recomputed raw-byte pins and actual H6-A identity types.
    """
    if type(source_pins) is not H4SourcePins:
        raise TypeError("source_pins must be an exact H4SourcePins")
    attempts = tuple(f"S3-{i:02d}" for i in range(24)) + tuple(
        f"S4-{i:02d}" for i in range(24)
    )
    payload: dict[str, object] = {
        "schema_version": "rob974_h4_fixture_plan_v1",
        "production_state": "fixture_non_production_pending_actual_h6a",
        "research_document_sha256": RESEARCH_DOCUMENT_SHA256,
        "window": [WINDOW_START_MS, WINDOW_END_MS],
        "universe": list(SYMBOLS),
        "pairs": list(PAIRS),
        "folds": _fold_payload(),
        "strategies": [_strategy_payload("S3"), _strategy_payload("S4")],
        "logical_attempt_ids": list(attempts),
        "logical_attempt_count": 48,
        "scenarios": list(SCENARIOS),
        "funding": {
            "gate_order": "h3_global_arbitration_then_exact_entry_then_funding_then_h2_open",
            "strict_expected_debit_limit_bps": 3.0,
            "s3_missing": "funding_evidence_unavailable",
            "s4_requires_both_legs": True,
            "realized_interval": "[entry,exit)",
            "realized_weighting": "one_time_entry_frozen_basket_weighted",
        },
        "h2_execution_authority": {
            "S3": "rob974_h2_s3_engine",
            "S4": "rob974_h2_s4_engine_historical_only",
        },
        "scorecard": scorecard_contract(),
        "verdict": campaign_verdict_contract(),
        "evidence": {
            "unique_generator_schema": "rob974-h3-generator-evidence-v1",
            "unique_before_horizon_funding_engine": True,
            "path_scenarios": list(SCENARIOS),
            "reasons": {
                "horizon": [
                    "insufficient_train_exit_horizon",
                    "insufficient_oos_exit_horizon",
                    "insufficient_pbo_exit_horizon",
                ],
                "non_selected": "not_selected",
                "aggregate_never_selected": "never_selected",
            },
        },
        "pbo": {
            "window": [WINDOW_START_MS, WINDOW_END_MS],
            "scenario": PBO_SCENARIO,
            "configs_per_strategy": 24,
            "days_per_config": PBO_DAYS,
            "slices": PBO_SLICES,
            "reference_only": True,
        },
        "source_pins": {
            "runner_bundle_sha256": source_pins.runner_bundle_sha256,
            "pbo_source_sha256": source_pins.pbo_source_sha256,
        },
    }
    return H4FixturePlan(payload, canonical_sha256(payload), attempts)


__all__ = ["H4FixturePlan", "build_fixture_plan"]
