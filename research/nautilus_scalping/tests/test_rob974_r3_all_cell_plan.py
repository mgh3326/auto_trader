"""ROB-974 R3 production-shaped identity and all-cell fan-out tests."""

from __future__ import annotations

import dataclasses
from collections import Counter

import pytest
import rob974_r3_h3_adapter as h3_adapter
import rob974_r3_manifest as manifest
import rob974_r3_plan as plan_module
import rob974_r3_postaudit as postaudit
from rob974_h2_dtos import Z_ENTRY_ABS_MIN
from rob974_h4_contracts import SCENARIOS, exact_h4_folds
from rob974_h4_h6a_adapter import RUNNER_SOURCE_FILES, build_production_h4_plan
from rob974_r3_h4_s4_adapter import R3_ENGINE_SOURCE_FILES
from rob974_r3_shape import R3_CANONICAL_ROW_ORDER, compute_exact_12_mapping_hash


def _s4_estimate(config_id: str, **changes):
    values = {
        "config_id": config_id,
        "decision_ts": 144_000_000,
        "pair": "XRP-DOGE",
        "symbol_a": "XRPUSDT",
        "symbol_b": "DOGEUSDT",
        "beta_a": 1.0,
        "beta_b": 1.0,
        "beta_a_first": 1.0,
        "beta_a_second": 1.0,
        "beta_b_first": 1.0,
        "beta_b_second": 1.0,
        "weight_a": 0.5,
        "weight_b": 0.5,
        "spread": 0.018,
        "mu": 0.0,
        "mad": 0.01,
        "effective_mad_scale": 0.014826,
        "z": 0.60,
        "prior_beta_a": 1.0,
        "prior_beta_b": 1.0,
        "prior_weight_a": 0.5,
        "prior_weight_b": 0.5,
        "prior_mu": 0.0,
        "prior_mad": 0.01,
        "prior_effective_mad_scale": 0.014826,
        "z_prior": 0.80,
        "D_fraction": 0.018,
        "D_bps": 180.0,
        "rho": 0.60,
        "phi": 0.75,
        "half_life_4h_bars": 2.0,
        "beta_stability": 0.20,
        "sigma_pair": 0.0,
        "pair_return_fraction": -0.001,
        "pair_return_bps": -10.0,
        "current_market_return_4h": 0.50,
    }
    values.update(changes)
    return h3_adapter.s4.S4Estimate(**values)


def test_production_plan_issues_real_exact_12_identity_over_eight_real_folds() -> None:
    plan = plan_module.build_production_r3_plan()
    rebuilt = plan_module.build_production_r3_plan()

    assert tuple(spec.row_id for spec in plan.row_specs) == R3_CANONICAL_ROW_ORDER
    assert plan.manifest_rows == manifest.FROZEN_R3_ROSTER
    assert plan.folds == exact_h4_folds()
    assert len(plan.folds) == 8
    assert plan.phases == ("train", "oos")
    assert plan.scenarios == SCENARIOS
    assert tuple(row_id for row_id, _ in plan.ordered_mapping) == (
        R3_CANONICAL_ROW_ORDER
    )
    assert len({experiment_id for _, experiment_id in plan.ordered_mapping}) == 12
    assert all(len(experiment_id) == 64 for _, experiment_id in plan.ordered_mapping)
    assert plan.exact_12_mapping_hash == compute_exact_12_mapping_hash(
        plan.ordered_mapping
    )
    assert len(plan.full_campaign_hash) == 64
    assert plan.campaign_run_id.startswith("rob974r3-")
    assert plan.campaign_run_id == postaudit.derive_r3_postaudit_campaign_run_id(
        plan.full_campaign_hash
    )
    assert rebuilt.full_campaign_hash == plan.full_campaign_hash
    assert rebuilt.campaign_run_id == plan.campaign_run_id
    assert rebuilt.ordered_mapping == plan.ordered_mapping
    assert all(len(value) == 64 for value in plan.source_pins.as_dict().values())

    payload = plan.to_payload()
    assert payload["preregistration_document_sha256"] == (
        manifest.PREREGISTRATION_DOCUMENT_SHA256
    )
    assert payload["execution"]["selection"] == "none_all_preregistered_cells"
    assert payload["execution"]["oos_threshold_feedback"] is False
    assert payload["execution"]["candidate_batches"] == 12 * 8 * 2
    assert payload["execution"]["engine_invocations"] == 12 * 8 * 2 * 3
    assert payload["production_state"] == "identity_ready_execution_enabled"
    assert payload["execution"]["operational_status"] == "COMPLETE"
    assert payload["execution"]["production_execution_enabled"] is True
    assert payload["execution"]["blocker_reason"] is None
    assert payload["execution"]["affected_config_ids"] == []
    assert payload["execution"]["frozen_r2_h2_observed_z_abs_min"] == 1.0
    assert payload["execution"]["r3_observed_z_abs_min"] == 0.60
    assert payload["execution"]["r3_s4_execution_lineage"] == (
        "rob974.r3.s4.signed_observed_z.v1"
    )

    assert plan.source_pins.engine_source_sha256 == (
        plan_module.source_bundle_sha256(R3_ENGINE_SOURCE_FILES)
    )
    assert plan.source_pins.engine_source_sha256 != (
        build_production_h4_plan().source_pins.engine_source_sha256
    )

    assert build_production_h4_plan().full_campaign_hash == (
        "2c47864c7ab661f16be6c414a1140944ec36832bb268e86183555b56c6f85f53"
    )


def test_runner_source_pin_covers_every_wave_one_production_boundary() -> None:
    logical_paths = tuple(
        path for path, _physical in plan_module.R3_RUNNER_SOURCE_FILES
    )
    required = (
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
        "research/nautilus_scalping/rob974_r3_scorecard.py",
        "research/nautilus_scalping/rob974_r3_markdown.py",
        "research/nautilus_scalping/rob974_r3_h4_s4_adapter.py",
        "research/nautilus_scalping/rob974_r3_plan.py",
        "research/nautilus_scalping/rob974_r3_postaudit.py",
        "app/services/rob974_r3_h6a_bridge.py",
        "app/services/rob974_r3_materializer.py",
    )
    assert all(logical_path in logical_paths for logical_path in required)
    r2_logical_paths = {path for path, _physical in RUNNER_SOURCE_FILES}
    assert (
        tuple(path for path in logical_paths if path not in r2_logical_paths)
        == required
    )
    assert len(logical_paths) == len(set(logical_paths))


def test_plan_rejects_partial_mapping_fold_source_and_hash_drift() -> None:
    plan = plan_module.build_production_r3_plan()
    mutants = (
        {"ordered_mapping": plan.ordered_mapping[:-1]},
        {"folds": plan.folds[:-1]},
        {"full_campaign_hash": "f" * 64},
    )
    for changes in mutants:
        with pytest.raises(plan_module.R3PlanError):
            dataclasses.replace(plan, **changes)
    with pytest.raises(plan_module.R3PlanError):
        dataclasses.replace(plan.source_pins, runner_source_sha256="0" * 64)


@pytest.mark.parametrize("family", ("S3", "S4"))
def test_pbo_helper_accepts_only_exact_family_roster(family: str) -> None:
    expected = (
        manifest.FROZEN_R3_S3_CONFIGS
        if family == "S3"
        else manifest.FROZEN_R3_S4_CONFIGS
    )
    assert plan_module.validate_r3_pbo_roster(family, expected) == expected
    mutants = (
        expected[:-1],
        (*expected, expected[-1]),
        tuple(reversed(expected)),
        tuple(row.config_id for row in expected),
    )
    for mutant in mutants:
        with pytest.raises(plan_module.R3PlanError):
            plan_module.validate_r3_pbo_roster(family, mutant)


def test_nonproduction_topology_runs_train_and_oos_without_winner_selection() -> None:
    plan = plan_module.build_production_r3_plan()
    candidate_calls: list[tuple[str, str, str]] = []
    candidate_buffers: list[plan_module.R3CandidateBuffer] = []
    engine_tokens: list[object] = []

    def candidates(phase, config, fold):
        candidate_calls.append((phase, config.config_id, fold.fold_id))
        batch = plan_module.R3CandidateBuffer(
            phase=phase,
            row_id=config.config_id,
            fold_id=fold.fold_id,
            candidates=((phase, config.config_id, fold.fold_id),),
        )
        candidate_buffers.append(batch)
        return batch

    def fresh_engine(key):
        token = object()
        engine_tokens.append(token)
        return plan_module.R3FreshEngine(
            invocation=key,
            state_token=token,
            execute=lambda batch, key=key: (key, batch),
        )

    results = plan_module.simulate_r3_all_cell_topology(
        plan,
        candidate_factory=candidates,
        engine_factory=fresh_engine,
    )
    assert len(candidate_calls) == 12 * 8 * 2
    assert len({id(batch) for batch in candidate_buffers}) == len(candidate_buffers)
    assert len(results) == 12 * 8 * 2 * 3
    assert len({id(token) for token in engine_tokens}) == len(engine_tokens)
    assert Counter(result.invocation.phase for result in results) == {
        "train": 12 * 8 * 3,
        "oos": 12 * 8 * 3,
    }
    assert Counter(result.invocation.scenario for result in results) == dict.fromkeys(
        SCENARIOS, 12 * 8 * 2
    )
    assert (
        tuple(
            result.invocation.row_id
            for result in results
            if result.invocation.phase == "oos"
            and result.invocation.fold_id == "fold-00"
            and result.invocation.scenario == SCENARIOS[0]
        )
        == R3_CANONICAL_ROW_ORDER
    )


def test_nonproduction_topology_rejects_shared_buffer_and_engine_state() -> None:
    plan = plan_module.build_production_r3_plan()
    shared_batch: plan_module.R3CandidateBuffer | None = None

    def shared_candidates(phase, config, fold):
        nonlocal shared_batch
        if shared_batch is None:
            shared_batch = plan_module.R3CandidateBuffer(
                phase=phase,
                row_id=config.config_id,
                fold_id=fold.fold_id,
                candidates=(),
            )
        return shared_batch

    def fresh_engine(key):
        return plan_module.R3FreshEngine(
            invocation=key,
            state_token=object(),
            execute=lambda batch: batch,
        )

    with pytest.raises(plan_module.R3PlanError):
        plan_module.simulate_r3_all_cell_topology(
            plan,
            candidate_factory=shared_candidates,
            engine_factory=fresh_engine,
        )

    shared_token = object()

    def fresh_candidates(phase, config, fold):
        return plan_module.R3CandidateBuffer(
            phase=phase,
            row_id=config.config_id,
            fold_id=fold.fold_id,
            candidates=(),
        )

    def shared_engine_state(key):
        return plan_module.R3FreshEngine(
            invocation=key,
            state_token=shared_token,
            execute=lambda batch: batch,
        )

    with pytest.raises(plan_module.R3PlanError):
        plan_module.simulate_r3_all_cell_topology(
            plan,
            candidate_factory=fresh_candidates,
            engine_factory=shared_engine_state,
        )


def test_production_execution_reuses_exact_all_cell_topology() -> None:
    plan = plan_module.build_production_r3_plan()
    candidate_calls: list[tuple[str, str, str]] = []
    engine_calls: list[plan_module.R3InvocationKey] = []

    def candidates(phase, config, fold):
        candidate_calls.append((phase, config.config_id, fold.fold_id))
        return plan_module.R3CandidateBuffer(
            phase=phase,
            row_id=config.config_id,
            fold_id=fold.fold_id,
            candidates=((phase, config.config_id, fold.fold_id),),
        )

    def fresh_engine(key):
        engine_calls.append(key)
        return plan_module.R3FreshEngine(
            invocation=key,
            state_token=object(),
            execute=lambda batch, key=key: (key, batch),
        )

    results = plan_module.run_r3_all_cell_campaign(
        plan,
        candidate_factory=candidates,
        engine_factory=fresh_engine,
    )
    assert len(candidate_calls) == 12 * 8 * 2
    assert len(engine_calls) == len(results) == 12 * 8 * 2 * 3
    assert tuple(result.invocation for result in results) == tuple(engine_calls)


def test_low_z_r3_candidate_is_blocked_before_frozen_h2_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Z_ENTRY_ABS_MIN == 1.0
    config = manifest.get_r3_config("S4-R3-06")
    outcome = h3_adapter.evaluate_r3_s4_gates(_s4_estimate(config.config_id), config)
    assert outcome.candidate is not None
    assert abs(outcome.candidate.observed_z) == 0.60

    delegate_calls: list[object] = []

    def unexpected_delegate(candidate, *, fold_id):
        delegate_calls.append(candidate)
        raise AssertionError("incompatible R3 candidate reached frozen H2 adapter")

    monkeypatch.setattr(
        h3_adapter.h3_h2_adapter, "adapt_s4_candidate", unexpected_delegate
    )
    with pytest.raises(h3_adapter.R3H2ExecutionSeamBlocked):
        h3_adapter.adapt_r3_s4_candidate_for_h2(outcome.candidate, fold_id="fold-00")
    assert delegate_calls == []
