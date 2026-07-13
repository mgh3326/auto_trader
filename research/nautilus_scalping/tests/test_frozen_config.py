"""ROB-351 (eng-review ex-ante enforcement) — frozen campaign config + hash.

Thresholds and the achievable-execution envelope are committed in PR1 BEFORE any
PR2 OOS read. The run records ``config_hash``; changing a threshold after the
fact changes the hash, making an ex-post tweak detectable rather than a promise.
"""

import ast
from pathlib import Path

import canonical_hash as ch
import frozen_config as fc

from research_contracts.evaluation_windows import ClosedWindow


def test_frozen_default_present_and_documented():
    c = fc.FROZEN_CONFIG
    assert c.economic_triviality_floor_bps > 0.0  # sign>0 is too low (Codex)
    assert c.achievable_maker_bps == 2.0  # Binance USD-M demo maker
    assert c.taker_bps == 4.0  # Binance USD-M demo taker
    assert c.fdr_alpha == 0.05


def test_config_hash_is_stable_across_calls():
    assert fc.FROZEN_CONFIG.config_hash() == fc.FROZEN_CONFIG.config_hash()


def test_config_hash_uses_registry_typed_canonical_authority():
    assert fc.FROZEN_CONFIG.config_hash() == ch.canonical_sha256(
        fc.FROZEN_CONFIG.to_dict()
    )


def test_changing_a_threshold_changes_the_hash():
    import dataclasses

    base = fc.FROZEN_CONFIG
    tweaked = dataclasses.replace(base, economic_triviality_floor_bps=999.0)
    assert tweaked.config_hash() != base.config_hash()


def test_evaluation_windows_are_frozen_in_config_and_policy_hashes():
    import dataclasses

    base = fc.FROZEN_CONFIG
    changed_windows = dataclasses.replace(
        base.evaluation_windows,
        sealed_oos=ClosedWindow(start="2026-02-02", end="2026-03-22"),
    )
    tweaked = dataclasses.replace(base, evaluation_windows=changed_windows)

    assert tweaked.config_hash() != base.config_hash()
    assert tweaked.policy_identity() != base.policy_identity()
    assert tweaked.policy_identity()["evaluation_windows"] == changed_windows.to_dict()


def test_honest_gate_definitions_are_frozen_in_campaign_hash():
    import dataclasses

    base = fc.FROZEN_CONFIG
    fields = {
        "dsr_probability_threshold": 0.99,
        "pbo_max": 0.4,
        "baseline_names": ("cash",),
        "random_baseline_seed": 848,
        "random_baseline_repetitions": 101,
        "cost_stress_multipliers": (2.0,),
        "trial_sharpe_method": "median_cv_fold_sharpe",
        "trial_p_value_method": "one_sided_t_cv_fold_sharpe",
        "selection_score_method": "alternate_validation_score",
        "trial_runner": "other-runner",
        "trial_timeframe": "5m",
        "trial_evidence_schema_version": "honest_trial.v4",
        "trial_evidence_producer": "other-producer",
        "trial_evidence_producer_version": "2",
        "trial_min_folds": 3,
        "mdd_target_pct": 5.0,
    }
    for name, value in fields.items():
        assert (
            dataclasses.replace(base, **{name: value}).config_hash()
            != base.config_hash()
        )


def test_to_dict_round_trip():
    c = fc.FROZEN_CONFIG
    assert fc.CampaignConfig.from_dict(c.to_dict()) == c


def test_identity_definitions_bind_all_gate_provenance_components():
    config = fc.FROZEN_CONFIG
    assert config.benchmark_identity() == {
        "names": list(config.baseline_names),
        "same_turnover_random": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
    }
    assert config.cost_identity() == {
        "taker_bps": config.taker_bps,
        "half_spread_bps": config.half_spread_bps,
        "slippage_bps": config.slippage_bps,
        "stress_multipliers": list(config.cost_stress_multipliers),
    }
    assert config.policy_identity() == {
        "schema_version": "honest_offline_gate.v1",
        "evaluation_windows": config.evaluation_windows.to_dict(),
        "selection": {
            "evidence": "validation_only",
            "score_method": config.selection_score_method,
            "tie_break": "parameter_key_ascending",
            "ties": "non_promotable",
            "sealed_oos": "finalize_only",
        },
        "trial_statistics": {
            "runner": config.trial_runner,
            "timeframe": config.trial_timeframe,
            "evidence_schema_version": config.trial_evidence_schema_version,
            "producer": config.trial_evidence_producer,
            "producer_version": config.trial_evidence_producer_version,
            "sharpe_method": config.trial_sharpe_method,
            "p_value_method": config.trial_p_value_method,
            "min_folds": config.trial_min_folds,
        },
        "dsr": {
            "probability_threshold": config.dsr_probability_threshold,
            "min_observations": config.dsr_min_observations,
        },
        "pbo": {"slices": config.pbo_slices, "maximum": config.pbo_max},
        "fdr": {"alpha": config.fdr_alpha},
        "economic_edge": {"minimum_bps": config.economic_triviality_floor_bps},
        "pit": {
            "manifest_required": True,
            "information_cutoff_required": True,
            "campaign_cutoff_match": "exact",
        },
        "finalization": {
            "one_time_per_run": True,
            "invalid_evidence": "non_promotable",
        },
    }
    assert config.mdd_identity() == {"target_pct": config.mdd_target_pct}


def test_frozen_config_keeps_isolated_stdlib_boundary():
    path = Path(fc.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not {module for module in imported if module.startswith("app.")}
