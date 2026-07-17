"""ROB-946 (H6, ROB-940) — pure campaign-identity builder: RED-first coverage.

This module is deliberately GENERIC and manifest-injected (ROB-946 §9): it does
not hardcode the production 24-row campaign (that is H3's authority once
merged). The exact Fable-approved 24 rows below are a TEST FIXTURE only, used
to prove the generic builder (a) reproduces the exact frozen values/order when
fed them, and (b) fails closed on every malformed campaign shape a real caller
could accidentally supply (13th row, missing, duplicate, wrong count, stale
source hash, tampered dataset manifest).
"""

from __future__ import annotations

import json
from pathlib import Path

import canonical_hash
import pytest
import rob941_frozen_scope as frozen
from rob940_cost_model import COST_SCENARIOS, MIN_TP_DISTANCE_BPS
from rob946_campaign_identity import (
    CampaignConfigRow,
    CampaignExperimentSpec,
    CampaignIdentityError,
    CampaignRowCountError,
    CampaignRowIdError,
    DatasetManifestHashMismatchError,
    StrategySourceMismatchError,
    StrategySourceProvenance,
    build_campaign_experiment_specs,
    build_universe_component,
    validate_campaign_rows,
    validate_same_strategy_components_are_identical,
)

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "data_manifests"
    / "rob941_corpus_manifest.v1.json"
)
_EXPECTED_MANIFEST_HASH = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)

# Fable Q1=A, frozen 2026-07-17 (strategy-fable-consult-20260717-123049.md +
# orch-fable-answer-strategy-20260717.md) — exact, no modifications.
_S1_ROWS: tuple[tuple[str, dict, str], ...] = (
    ("S1-00", {"L": 16, "q_min": 1.25, "k_SL": 1.25, "R_TP": 1.80}, "연구 default"),
    (
        "S1-01",
        {"L": 12, "q_min": 1.25, "k_SL": 1.25, "R_TP": 1.80},
        "shorter breakout lookback",
    ),
    (
        "S1-02",
        {"L": 24, "q_min": 1.25, "k_SL": 1.25, "R_TP": 1.80},
        "longer breakout lookback",
    ),
    (
        "S1-03",
        {"L": 16, "q_min": 1.00, "k_SL": 1.25, "R_TP": 1.80},
        "looser volume confirmation",
    ),
    (
        "S1-04",
        {"L": 16, "q_min": 1.50, "k_SL": 1.25, "R_TP": 1.80},
        "stricter volume confirmation",
    ),
    ("S1-05", {"L": 16, "q_min": 1.25, "k_SL": 1.00, "R_TP": 1.80}, "tighter ATR stop"),
    ("S1-06", {"L": 16, "q_min": 1.25, "k_SL": 1.50, "R_TP": 1.80}, "wider ATR stop"),
    (
        "S1-07",
        {"L": 16, "q_min": 1.25, "k_SL": 1.25, "R_TP": 1.50},
        "lower payoff ratio",
    ),
    (
        "S1-08",
        {"L": 16, "q_min": 1.25, "k_SL": 1.25, "R_TP": 2.00},
        "higher payoff ratio",
    ),
    (
        "S1-09",
        {"L": 12, "q_min": 1.50, "k_SL": 1.25, "R_TP": 1.80},
        "fast breakout requires stronger volume",
    ),
    (
        "S1-10",
        {"L": 24, "q_min": 1.00, "k_SL": 1.25, "R_TP": 1.80},
        "slow breakout tolerates weaker volume",
    ),
    (
        "S1-11",
        {"L": 16, "q_min": 1.25, "k_SL": 1.00, "R_TP": 2.00},
        "tight-stop/high-payoff cost resilience",
    ),
)
_S2_ROWS: tuple[tuple[str, dict, str], ...] = (
    (
        "S2-00",
        {"z_min": 3.00, "v_min": 2.00, "ER_max": 0.35, "R_min": 1.25},
        "연구 default",
    ),
    (
        "S2-01",
        {"z_min": 2.75, "v_min": 2.00, "ER_max": 0.35, "R_min": 1.25},
        "lower shock threshold",
    ),
    (
        "S2-02",
        {"z_min": 3.25, "v_min": 2.00, "ER_max": 0.35, "R_min": 1.25},
        "higher shock threshold",
    ),
    (
        "S2-03",
        {"z_min": 3.00, "v_min": 1.50, "ER_max": 0.35, "R_min": 1.25},
        "looser volume spike",
    ),
    (
        "S2-04",
        {"z_min": 3.00, "v_min": 2.50, "ER_max": 0.35, "R_min": 1.25},
        "stricter volume spike",
    ),
    (
        "S2-05",
        {"z_min": 3.00, "v_min": 2.00, "ER_max": 0.25, "R_min": 1.25},
        "stricter mean-reversion regime",
    ),
    (
        "S2-06",
        {"z_min": 3.00, "v_min": 2.00, "ER_max": 0.45, "R_min": 1.25},
        "looser regime filter",
    ),
    (
        "S2-07",
        {"z_min": 3.00, "v_min": 2.00, "ER_max": 0.35, "R_min": 1.20},
        "lower reward floor",
    ),
    (
        "S2-08",
        {"z_min": 3.00, "v_min": 2.00, "ER_max": 0.35, "R_min": 1.35},
        "higher reward floor",
    ),
    (
        "S2-09",
        {"z_min": 2.75, "v_min": 1.50, "ER_max": 0.45, "R_min": 1.20},
        "permissive/frequency frontier",
    ),
    (
        "S2-10",
        {"z_min": 3.25, "v_min": 2.50, "ER_max": 0.25, "R_min": 1.35},
        "selective/quality frontier",
    ),
    (
        "S2-11",
        {"z_min": 2.75, "v_min": 2.50, "ER_max": 0.25, "R_min": 1.25},
        "lower z only when volume/regime are strict",
    ),
)


def _rows(s1=_S1_ROWS, s2=_S2_ROWS) -> list[CampaignConfigRow]:
    out = []
    for config_id, params, hyp in s1:
        out.append(
            CampaignConfigRow(config_id=config_id, params=dict(params), hypothesis=hyp)
        )
    for config_id, params, hyp in s2:
        out.append(
            CampaignConfigRow(config_id=config_id, params=dict(params), hypothesis=hyp)
        )
    return out


def _sources() -> dict[str, StrategySourceProvenance]:
    s1_text = "def donchian_15m_signal(): ...  # S1 placeholder pending H3"
    s2_text = (
        "def confirmed_shock_reversal_5m_signal(): ...  # S2 placeholder pending H3"
    )
    return {
        "S1": StrategySourceProvenance(
            strategy_key="ROB940-S1-DONCHIAN-15M",
            strategy_version="s1-v1",
            source_text=s1_text,
        ),
        "S2": StrategySourceProvenance(
            strategy_key="ROB940-S2-SHOCK-REVERSAL-5M",
            strategy_version="s2-v1",
            source_text=s2_text,
        ),
    }


def _dataset_manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def _build(
    rows=None,
    sources=None,
    dataset_manifest=None,
    expected_hash=_EXPECTED_MANIFEST_HASH,
):
    return build_campaign_experiment_specs(
        rows if rows is not None else _rows(),
        sources=sources if sources is not None else _sources(),
        dataset_manifest=dataset_manifest
        if dataset_manifest is not None
        else _dataset_manifest(),
        dataset_manifest_expected_hash=expected_hash,
    )


# --------------------------------------------------------------------------- #
# Exact 24 rows: IDs, order, values                                            #
# --------------------------------------------------------------------------- #


def test_exact_24_config_ids_in_canonical_order():
    specs = _build()
    ids = [s.components["params"]["config_id"] for s in specs]
    expected = [c for c, _, _ in _S1_ROWS] + [c for c, _, _ in _S2_ROWS]
    assert ids == expected
    assert len(ids) == 24


def test_exact_params_and_hypothesis_per_row():
    specs = _build()
    by_id = {s.components["params"]["config_id"]: s for s in specs}
    for config_id, params, hyp in (*_S1_ROWS, *_S2_ROWS):
        got = by_id[config_id].components["params"]
        assert got["hypothesis"] == hyp
        for key, value in params.items():
            assert got[key] == value


def test_all_24_derived_experiment_ids_are_unique():
    specs = _build()
    ids = {
        canonical_hash.derive_experiment_id(
            s.strategy_key,
            s.strategy_version,
            canonical_hash.compute_identity_hashes(s.components),
        )
        for s in specs
    }
    assert len(ids) == 24


# --------------------------------------------------------------------------- #
# Same-strategy: params-only variation                                        #
# --------------------------------------------------------------------------- #


def test_same_strategy_12_configs_share_every_component_except_params():
    specs = _build()
    validate_same_strategy_components_are_identical(specs)  # must not raise


def test_validator_actually_detects_injected_component_drift():
    # A real regression guard, not a vacuous positive: build_campaign_experiment_specs
    # reuses the SAME object for every common component, so equality holds by
    # construction there. This proves validate_same_strategy_components_are_identical
    # is independently load-bearing — it must reject a list an EXTERNAL builder
    # (e.g. a future H3 manifest) assembled with per-row component drift.
    specs = list(_build())
    drifted = specs[1]
    corrupted = CampaignExperimentSpec(
        strategy_key=drifted.strategy_key,
        strategy_version=drifted.strategy_version,
        hypothesis=drifted.hypothesis,
        components={
            **drifted.components,
            "frozen_config": {
                **drifted.components["frozen_config"],
                "timeout_bars": 999,
            },
        },
    )
    specs[1] = corrupted
    with pytest.raises(CampaignIdentityError):
        validate_same_strategy_components_are_identical(specs)


def test_s1_and_s2_differ_in_strategy_key_version_and_code_hash():
    specs = _build()
    s1 = next(s for s in specs if s.components["params"]["config_id"] == "S1-00")
    s2 = next(s for s in specs if s.components["params"]["config_id"] == "S2-00")
    assert s1.strategy_key != s2.strategy_key
    assert s1.strategy_version != s2.strategy_version
    assert s1.components["code"] != s2.components["code"]
    # non-params components besides code/strategy/frozen_config are common
    # across BOTH strategies (shared corpus/cost/policy) — only these three
    # (plus params) are allowed to differ between S1 and S2.
    for name in (
        "dataset_manifest",
        "universe",
        "pit",
        "policy",
        "benchmark",
        "cost",
        "mdd",
    ):
        assert s1.components[name] == s2.components[name], name


# --------------------------------------------------------------------------- #
# Fail-closed: row-shape violations                                           #
# --------------------------------------------------------------------------- #


def test_23_rows_rejected():
    with pytest.raises(CampaignRowCountError):
        _build(rows=_rows()[:-1])


def test_25_rows_rejected():
    rows = _rows()
    extra = CampaignConfigRow(
        config_id="S1-12", params={"L": 99}, hypothesis="13th row"
    )
    with pytest.raises(CampaignRowCountError):
        _build(rows=[*rows, extra])


def test_duplicate_config_id_rejected():
    rows = _rows()
    rows[1] = CampaignConfigRow(
        config_id=rows[0].config_id,
        params=rows[1].params,
        hypothesis=rows[1].hypothesis,
    )
    with pytest.raises(CampaignRowIdError):
        validate_campaign_rows(rows)


def test_missing_config_id_in_sequence_rejected():
    rows = _rows()
    rows[0] = CampaignConfigRow(
        config_id="S1-00b", params=rows[0].params, hypothesis=rows[0].hypothesis
    )
    with pytest.raises(CampaignRowIdError):
        validate_campaign_rows(rows)


def test_81_grid_style_count_rejected():
    # A full 3^4 per-strategy grid (81) is explicitly banned by the research
    # brief; feeding it in must fail on row-count alone.
    grid_rows = [
        CampaignConfigRow(config_id=f"S1-{i:02d}", params={"L": i}, hypothesis="grid")
        for i in range(81)
    ]
    with pytest.raises(CampaignRowCountError):
        validate_campaign_rows(grid_rows)


def test_universe_component_has_no_symbol_override_hook():
    import inspect

    assert dict(inspect.signature(build_universe_component).parameters) == {}
    assert build_universe_component() == build_universe_component()
    assert build_universe_component()["symbols"] == list(frozen.UNIVERSE)


# --------------------------------------------------------------------------- #
# Fail-closed: dataset manifest / strategy source integrity                   #
# --------------------------------------------------------------------------- #


def test_dataset_manifest_hash_must_match_committed_rob941_manifest():
    specs = _build()
    assert specs[0].components["dataset_manifest"] == _dataset_manifest()


def test_tampered_dataset_manifest_rejected():
    tampered = _dataset_manifest()
    tampered["window_start_iso"] = "2020-01-01T00:00:00Z"
    with pytest.raises(DatasetManifestHashMismatchError):
        _build(dataset_manifest=tampered)


def test_wrong_expected_hash_rejected_even_if_manifest_itself_is_untampered():
    with pytest.raises(DatasetManifestHashMismatchError):
        _build(expected_hash="0" * 64)


def test_stale_asserted_source_hash_rejected():
    sources = _sources()
    sources["S1"] = StrategySourceProvenance(
        strategy_key=sources["S1"].strategy_key,
        strategy_version=sources["S1"].strategy_version,
        source_text=sources["S1"].source_text,
        expected_source_sha256="0" * 64,
    )
    with pytest.raises(StrategySourceMismatchError):
        _build(sources=sources)


def test_matching_asserted_source_hash_is_accepted():
    sources = _sources()
    actual = sources["S1"].verified_source_sha256()
    sources["S1"] = StrategySourceProvenance(
        strategy_key=sources["S1"].strategy_key,
        strategy_version=sources["S1"].strategy_version,
        source_text=sources["S1"].source_text,
        expected_source_sha256=actual,
    )
    _build(sources=sources)  # must not raise


def test_s1_and_s2_source_provenance_must_not_collide():
    sources = _sources()
    sources["S2"] = StrategySourceProvenance(
        strategy_key=sources["S2"].strategy_key,
        strategy_version=sources["S2"].strategy_version,
        source_text=sources["S1"].source_text,  # identical bytes to S1
    )
    with pytest.raises(StrategySourceMismatchError):
        _build(sources=sources)


def test_missing_strategy_source_rejected():
    sources = _sources()
    del sources["S2"]
    with pytest.raises(CampaignIdentityError):
        _build(sources=sources)


# --------------------------------------------------------------------------- #
# Cost/policy components pin the frozen ROB-940 numeric contract              #
# --------------------------------------------------------------------------- #


def test_cost_component_pins_5_10_13_17_22_68():
    specs = _build()
    cost = specs[0].components["cost"]
    assert cost["fee_entry_bps"] == 5.0
    assert cost["fee_exit_bps"] == 5.0
    assert cost["fee_round_trip_bps"] == 10.0
    assert cost["scenarios"] == {s.name: s.all_in_bps for s in COST_SCENARIOS}
    assert cost["scenarios"] == {
        "base": 13.0,
        "primary_stress": 17.0,
        "upward_stress": 22.0,
    }
    assert cost["min_tp_distance_bps"] == 68.0 == MIN_TP_DISTANCE_BPS


def test_policy_component_pins_folds_selection_and_min_evidence_guards():
    specs = _build()
    policy = specs[0].components["policy"]
    assert policy["walk_forward"] == {
        "train_days": 120,
        "embargo_hours": 3,
        "oos_days": 28,
        "roll_days": 28,
        "min_folds": 6,
    }
    assert policy["selection"]["min_symbol_train_trades"] == 5
    assert policy["selection"]["min_eligible_symbols"] == 2
    assert policy["no_broker_execution"] is True


def test_build_is_pure_and_repeatable():
    specs_a = _build()
    specs_b = _build()
    assert specs_a == specs_b
