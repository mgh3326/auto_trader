"""ROB-974 R3 frozen manifest and shared H3 predicate contract tests."""

from __future__ import annotations

import dataclasses
import hashlib
import math
from pathlib import Path

import pytest
import rob974_h3_gate_predicates as predicates
import rob974_h3_s3 as s3
import rob974_h3_s4 as s4
import rob974_h4_h6a_adapter as r2_production
import rob974_r3_h3_adapter as adapter
import rob974_r3_manifest as manifest

_EXPECTED_S3 = (
    ("S3-R3-00", 16, 0.35, 0.35, 1.25, 1.60, 0.05, 0, "boundary"),
    ("S3-R3-01", 16, 0.35, 0.35, 1.25, 1.60, 0.00, 25, "boundary"),
    ("S3-R3-02", 16, 0.35, 0.35, 1.25, 1.60, 0.00, 0, "boundary"),
)
_EXPECTED_S4 = (
    ("S4-R3-00", 150, 1.10, 140, 1.25, 1.50, "boundary"),
    ("S4-R3-01", 150, 1.00, 160, 1.25, 1.50, "boundary"),
    ("S4-R3-02", 150, 1.00, 140, 1.25, 1.50, "boundary"),
    ("S4-R3-03", 150, 0.80, 180, 1.25, 1.50, "boundary"),
    ("S4-R3-04", 150, 0.80, 160, 1.25, 1.50, "boundary"),
    ("S4-R3-05", 150, 0.80, 140, 1.25, 1.50, "power"),
    ("S4-R3-06", 150, 0.60, 180, 1.25, 1.50, "boundary"),
    ("S4-R3-07", 150, 0.60, 160, 1.25, 1.50, "power"),
    ("S4-R3-08", 150, 0.60, 140, 1.25, 1.50, "power"),
)
_EXPECTED_RAYS = (
    ("S3-S-M0", "S3", "S_min", ("S3-R3-00", "S3-R3-02")),
    ("S3-M-S0", "S3", "M_min_bp", ("S3-R3-01", "S3-R3-02")),
    (
        "S4-Z-D140",
        "S4",
        "z_entry",
        ("S4-R3-00", "S4-R3-02", "S4-R3-05", "S4-R3-08"),
    ),
    (
        "S4-Z-D160",
        "S4",
        "z_entry",
        ("S4-R3-01", "S4-R3-04", "S4-R3-07"),
    ),
    ("S4-Z-D180", "S4", "z_entry", ("S4-R3-03", "S4-R3-06")),
    ("S4-D-Z1.0", "S4", "d_min_bp", ("S4-R3-01", "S4-R3-02")),
    (
        "S4-D-Z0.8",
        "S4",
        "d_min_bp",
        ("S4-R3-03", "S4-R3-04", "S4-R3-05"),
    ),
    (
        "S4-D-Z0.6",
        "S4",
        "d_min_bp",
        ("S4-R3-06", "S4-R3-07", "S4-R3-08"),
    ),
)


def _s3_values():
    return tuple(
        (
            row.config_id,
            row.L,
            row.q_min,
            row.ER_min,
            row.k_SL,
            row.R_TP,
            row.S_min,
            row.M_min_bp,
            row.planning_class,
        )
        for row in manifest.FROZEN_R3_S3_CONFIGS
    )


def _s4_values():
    return tuple(
        (
            row.config_id,
            row.W,
            row.z_entry,
            row.d_min_bp,
            row.k_SL,
            row.R_TP,
            row.planning_class,
        )
        for row in manifest.FROZEN_R3_S4_CONFIGS
    )


def test_exact_12_roster_anchors_exclusions_and_single_ray_authority() -> None:
    assert _s3_values() == _EXPECTED_S3
    assert _s4_values() == _EXPECTED_S4
    assert manifest.S3_R2_ANCHOR.as_tuple() == (
        "S3-03",
        16,
        0.35,
        0.35,
        1.25,
        1.60,
    )
    assert manifest.S4_R2_ANCHOR.as_tuple() == (
        "S4-02",
        150,
        1.25,
        1.50,
    )
    assert manifest.S3_EXCLUDED_CELLS == ((0.05, 25), (0.10, 0))
    assert manifest.S4_EXCLUDED_CELLS == ((1.20, 140), (1.00, 180))
    assert manifest.S4_SATURATION_Z_LT == 0.60
    assert manifest.S4_DUPLICATE_D_MIN_BP_LT == 140
    assert (
        tuple(
            (ray.ray_id, ray.family, ray.axis, ray.config_ids)
            for ray in manifest.R3_RELAXATION_RAYS
        )
        == _EXPECTED_RAYS
    )
    assert manifest.R3_ADJACENCY_EDGES == tuple(
        (left, right)
        for _ray_id, _family, _axis, config_ids in _EXPECTED_RAYS
        for left, right in zip(config_ids[:-1], config_ids[1:], strict=True)
    )


def test_manifest_contract_commits_prereg_roster_anchors_axes_and_graph() -> None:
    assert (
        manifest.PREREGISTRATION_DOCUMENT_SHA256
        == "b2f03a23285945c8fda84c56a040fe2466541e8250e0b01ea987ba9d315e7ac5"
    )
    payload = manifest.r3_manifest_contract_payload()
    assert payload["schema_version"] == manifest.R3_MANIFEST_CONTRACT_VERSION
    assert payload["preregistration_document_sha256"] == (
        manifest.PREREGISTRATION_DOCUMENT_SHA256
    )
    assert payload["moving_axes"] == {
        "S3": ["S_min", "M_min_bp"],
        "S4": ["z_entry", "d_min_bp"],
    }
    assert len(payload["roster"]) == 12
    assert payload["anchors"]
    assert payload["excluded_cells"]
    assert payload["relaxation_rays"]
    assert manifest.R3_MANIFEST_CONTRACT_HASH == (
        "40b194f436dd009102865ed35ba01c7d973231f5c409997e0867af531d7fbb41"
    )
    assert manifest.R3_S3_STRATEGY_CONTRACT.contract_hash == (
        "0bdfc36e13057076ce0fdd242c61f13be9e9ec01d78958d426ad4a1f46e7793f"
    )
    assert manifest.R3_S4_STRATEGY_CONTRACT.contract_hash == (
        "75ad9550edcd1571f7b69c686095bbcda8a8163cbd43394ea376118d8be49e27"
    )


def test_manifest_rejects_anchor_excluded_saturated_duplicate_reorder_and_rename() -> (
    None
):
    rows = manifest.FROZEN_R3_ROSTER
    excluded_s3 = dataclasses.replace(rows[0], S_min=0.05, M_min_bp=25)
    excluded_s4 = dataclasses.replace(rows[3], z_entry=1.20, d_min_bp=140)
    saturated_s4 = dataclasses.replace(rows[3], z_entry=0.50)
    duplicate_s4 = dataclasses.replace(rows[3], d_min_bp=120)
    mutants = (
        (dataclasses.replace(rows[0], L=12), *rows[1:]),
        (excluded_s3, *rows[1:]),
        (*rows[:3], excluded_s4, *rows[4:]),
        (*rows[:3], saturated_s4, *rows[4:]),
        (*rows[:3], duplicate_s4, *rows[4:]),
        (rows[1], rows[0], *rows[2:]),
        (dataclasses.replace(rows[0], config_id="S3-R3-99"), *rows[1:]),
    )
    for mutant in mutants:
        with pytest.raises(manifest.R3ManifestError):
            manifest.validate_r3_manifest(mutant)


def test_r2_anchor_and_engine_bytes_match_authorized_boundary_identity() -> None:
    manifest.validate_r2_anchor_projection()
    assert r2_production.build_production_h4_plan().full_campaign_hash == (
        "70f352c3c477e27a36111f1daa584deb4ca570ec57ae9555727d6bc6c68b4248"
    )
    root = Path(__file__).resolve().parents[1]
    assert hashlib.sha256(
        (root / "rob974_h2_s3_engine.py").read_bytes()
    ).hexdigest() == (
        "71c930aa18da414750733078fd0190e0442af5350b5641565aedec5e2105609a"
    )
    assert hashlib.sha256(
        (root / "rob974_h2_s4_engine.py").read_bytes()
    ).hexdigest() == (
        "2d4ca5eb37c78e75209eef66228ce78a3f9323058ec0ee1c34bf4b868aa495e9"
    )


def _s3_metrics(config_id: str, **changes) -> s3.S3Metrics:
    values = {
        "config_id": config_id,
        "decision_ts": 144_000_000,
        "symbol": "XRPUSDT",
        "R": 0.10,
        "ER": 0.35,
        "S": 0.05,
        "Qplus": 0.35,
        "Qminus": -0.10,
        "close": 101.0,
        "previous_close": 100.0,
        "prior_l_high": 102.0,
        "prior_l_low": 90.0,
        "atr20": 0.6,
        "A": 0.006,
        "vwap12": 100.0,
        "vwap24": 99.0,
        "percentile_30d": 20.0,
        "range24": 0.016 / 0.60,
        "market_return_24h": 0.0,
        "current_market_return_4h": -0.50,
        "bplus": 2,
        "bminus": 1,
    }
    values.update(changes)
    return s3.S3Metrics(**values)


def _s4_estimate(config_id: str, **changes) -> s4.S4Estimate:
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
        "z": 1.10,
        "prior_beta_a": 1.0,
        "prior_beta_b": 1.0,
        "prior_weight_a": 0.5,
        "prior_weight_b": 0.5,
        "prior_mu": 0.0,
        "prior_mad": 0.01,
        "prior_effective_mad_scale": 0.014826,
        "z_prior": 1.30,
        "D_fraction": 0.015,
        "D_bps": 150.0,
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
    return s4.S4Estimate(**values)


def test_s3_atomic_predicates_and_r3_adapter_use_inclusive_zero_without_epsilon() -> (
    None
):
    exact = predicates.evaluate_s3_threshold_predicates(
        market_return_24h=0.0,
        bplus=2,
        bminus=1,
        trend_strength=0.0,
        s_min=0.0,
        m_min_bp=0,
    )
    assert exact.market_direction == "long"
    assert exact.market_magnitude is True
    assert exact.market_breadth is True
    assert exact.trend_sign is True
    assert exact.trend_magnitude is True

    below_magnitude = predicates.evaluate_s3_threshold_predicates(
        market_return_24h=math.nextafter(25 / 10_000.0, -math.inf),
        bplus=2,
        bminus=1,
        trend_strength=0.0,
        s_min=0.0,
        m_min_bp=25,
    )
    assert below_magnitude.market_direction == "long"
    assert below_magnitude.market_magnitude is False
    assert below_magnitude.market_breadth is True
    assert below_magnitude.trend_sign is True
    assert below_magnitude.trend_magnitude is True

    m_config = manifest.get_r3_config("S3-R3-01")
    rejected = adapter.evaluate_r3_s3_gates(
        _s3_metrics(
            m_config.config_id,
            S=0.0,
            market_return_24h=math.nextafter(25 / 10_000.0, -math.inf),
        ),
        m_config,
    )
    assert rejected.side is None
    assert rejected.no_signal_reason == "market_regime"

    config = manifest.get_r3_config("S3-R3-02")
    outcome = adapter.evaluate_r3_s3_gates(_s3_metrics(config.config_id, S=0.0), config)
    assert outcome.candidate is not None
    assert outcome.candidate.config_id == "S3-R3-02"
    failed = adapter.evaluate_r3_s3_gates(
        _s3_metrics(config.config_id, S=math.nextafter(0.0, -math.inf)), config
    )
    assert failed.no_signal_reason == "trend_strength"


def test_s3_moving_threshold_exact_boundaries_pass_and_one_ulp_failing_side_fails() -> (
    None
):
    s_config = manifest.get_r3_config("S3-R3-00")
    assert adapter.evaluate_r3_s3_gates(
        _s3_metrics(s_config.config_id, S=0.05), s_config
    ).candidate
    assert (
        adapter.evaluate_r3_s3_gates(
            _s3_metrics(s_config.config_id, S=math.nextafter(0.05, -math.inf)),
            s_config,
        ).no_signal_reason
        == "trend_strength"
    )

    m_config = manifest.get_r3_config("S3-R3-01")
    boundary = 25 / 10_000.0
    assert adapter.evaluate_r3_s3_gates(
        _s3_metrics(m_config.config_id, S=0.0, market_return_24h=boundary),
        m_config,
    ).candidate
    assert (
        adapter.evaluate_r3_s3_gates(
            _s3_metrics(
                m_config.config_id,
                S=0.0,
                market_return_24h=math.nextafter(boundary, -math.inf),
            ),
            m_config,
        ).no_signal_reason
        == "market_regime"
    )


def test_s4_atomic_and_adapter_threshold_boundaries_are_inclusive() -> None:
    assert predicates.s4_prior_z_magnitude_passes(0.60, 0.60)
    assert not predicates.s4_prior_z_magnitude_passes(
        math.nextafter(0.60, -math.inf), 0.60
    )
    assert predicates.s4_current_z_magnitude_passes(-0.60, 0.60)
    assert not predicates.s4_current_z_magnitude_passes(
        math.nextafter(-0.60, math.inf), 0.60
    )
    assert predicates.s4_absolute_distance_passes(140.0, 140)
    assert not predicates.s4_absolute_distance_passes(
        math.nextafter(140.0, -math.inf), 140
    )
    assert predicates.s4_distance_to_tp_passes(0.015, 0.012)
    assert not predicates.s4_distance_to_tp_passes(
        math.nextafter(0.015, -math.inf), 0.012
    )

    z_config = manifest.get_r3_config("S4-R3-03")
    assert adapter.evaluate_r3_s4_gates(
        _s4_estimate(z_config.config_id, z=0.80, z_prior=1.00, D_bps=180.0),
        z_config,
    ).candidate
    assert (
        adapter.evaluate_r3_s4_gates(
            _s4_estimate(
                z_config.config_id,
                z=math.nextafter(0.80, -math.inf),
                z_prior=1.00,
                D_bps=180.0,
            ),
            z_config,
        ).no_signal_reason
        == "current_z_entry"
    )
    assert (
        adapter.evaluate_r3_s4_gates(
            _s4_estimate(
                z_config.config_id,
                z=0.80,
                z_prior=1.00,
                D_fraction=math.nextafter(0.018, -math.inf),
                D_bps=math.nextafter(180.0, -math.inf),
            ),
            z_config,
        ).no_signal_reason
        == "absolute_distance"
    )


def test_full_atomic_evaluators_and_candidate_first_fail_composition_are_identical() -> (
    None
):
    s3_config = manifest.get_r3_config("S3-R3-02")
    assert type(s3_config) is manifest.R3S3Config
    s3_metrics = _s3_metrics(s3_config.config_id, ER=0.34)
    s3_atoms = adapter.evaluate_r3_s3_atoms(s3_metrics, s3_config)
    assert len(s3_atoms.gate_results) == 11
    assert dict(s3_atoms.gate_results)["efficiency_ratio"] is False
    assert (
        adapter.evaluate_r3_s3_gates(s3_metrics, s3_config).no_signal_reason
        == "efficiency"
    )

    s4_config = manifest.get_r3_config("S4-R3-00")
    assert type(s4_config) is manifest.R3S4Config
    estimate = _s4_estimate(s4_config.config_id, rho=0.59, D_bps=150.0)
    observation = adapter.R3S4GateObservation.from_estimate(estimate)
    s4_atoms = adapter.evaluate_r3_s4_atoms(observation, s4_config)
    assert len(s4_atoms.gate_results) == 11
    assert dict(s4_atoms.gate_results)["rho"] is False
    assert dict(s4_atoms.gate_results)["d_min_distance"] is True
    assert dict(s4_atoms.gate_results)["notional_feasibility"] is True
    assert adapter.evaluate_r3_s4_gates(estimate, s4_config).no_signal_reason == "rho"
