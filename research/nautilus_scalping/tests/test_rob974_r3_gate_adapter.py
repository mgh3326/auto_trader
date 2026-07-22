from __future__ import annotations

import dataclasses
import json

import pytest
import rob974_h3_s3 as s3
import rob974_h3_s4 as s4
from rob944_folds import Fold
from rob974_features import FOUR_HOUR_MS, SYMBOLS
from rob974_h4_contracts import exact_h4_folds
from rob974_r3_evidence_context import (
    R3ProductionEvidenceContextError,
    issue_r3_production_evidence_context,
)
from rob974_r3_gate_adapter import (
    R3_GATE_UNIT_ID_VERSION,
    ProductionFoldGateSource,
    R3S4ObservationOutcome,
    build_production_gate_audit,
    build_production_gate_batches,
    canonical_gate_unit_id,
)
from rob974_r3_gate_metrics import GateAuditValidationError
from rob974_r3_h3_adapter import R3S4GateObservation
from rob974_r3_manifest import R3S3Config, R3S4Config, get_r3_config
from rob974_r3_plan import build_production_r3_plan

_EVIDENCE_CONTEXT = issue_r3_production_evidence_context(build_production_r3_plan())


def _fold(index: int) -> Fold:
    return exact_h4_folds()[index]


def _s3_metrics(config_id: str, decision_ts: int, symbol: str) -> s3.S3Metrics:
    return s3.S3Metrics(
        config_id=config_id,
        decision_ts=decision_ts,
        symbol=symbol,
        R=0.10,
        ER=0.50,
        S=0.10,
        Qplus=0.50,
        Qminus=-0.10,
        close=101.0,
        previous_close=100.0,
        prior_l_high=102.0,
        prior_l_low=90.0,
        atr20=0.6,
        A=0.006,
        vwap12=100.0,
        vwap24=99.0,
        percentile_30d=50.0,
        range24=0.03,
        market_return_24h=0.01,
        current_market_return_4h=0.005,
        bplus=3,
        bminus=0,
    )


def _s4_observation(
    config_id: str,
    decision_ts: int,
    pair: str,
    *,
    phi: float = 0.75,
) -> R3S4GateObservation:
    return R3S4GateObservation(
        config_id=config_id,
        decision_ts=decision_ts,
        pair=pair,
        phi=phi,
        z_prior=1.40,
        z_current=1.20,
        rho=0.70,
        half_life_4h_bars=2.409420839653209 if 0.0 < phi < 1.0 else None,
        beta_stability=0.10,
        d_bps=150.0,
        d_fraction=0.015,
        sigma_pair=0.0,
        weight_a=0.50,
        weight_b=0.50,
    )


def _decision_closes(fold: Fold, phase: str) -> tuple[int, ...]:
    if phase == "TRAIN":
        window = s3.EmitWindow(fold.train_start_ms, fold.train_end_ms)
    else:
        window = s3.EmitWindow(fold.oos_start_ms, fold.oos_end_ms)
    return s3.expected_decision_closes(window)


def _s3_sources(
    config: R3S3Config, phase: str = "TRAIN"
) -> tuple[ProductionFoldGateSource, ...]:
    return tuple(
        ProductionFoldGateSource(
            fold,
            tuple(
                s3.S3FormulaUnit(
                    decision_ts,
                    symbol,
                    _s3_metrics(config.config_id, decision_ts, symbol),
                )
                for decision_ts in _decision_closes(fold, phase)
                for symbol in SYMBOLS
            ),
        )
        for fold in (_fold(index) for index in range(8))
    )


def _s4_sources(config: R3S4Config) -> tuple[ProductionFoldGateSource, ...]:
    result: list[ProductionFoldGateSource] = []
    for index in range(8):
        fold = _fold(index)
        units: list[R3S4ObservationOutcome] = []
        for unit_index, (decision_ts, pair) in enumerate(
            item
            for ts in _decision_closes(fold, "TRAIN")
            for item in ((ts, p) for p in s4.PAIR_ORDER)
        ):
            if index == 0 and unit_index == 2:
                units.append(
                    R3S4ObservationOutcome(
                        decision_ts,
                        pair,
                        None,
                        "missing_required_context",
                    )
                )
            else:
                phi = 1.10 if index == 0 and unit_index == 0 else 0.75
                units.append(
                    R3S4ObservationOutcome(
                        decision_ts,
                        pair,
                        _s4_observation(config.config_id, decision_ts, pair, phi=phi),
                        None,
                    )
                )
        result.append(ProductionFoldGateSource(fold, tuple(units)))
    return tuple(result)


def test_s3_complete_source_grid_builds_exact_eight_canonical_batches() -> None:
    config = get_r3_config("S3-R3-02")
    assert type(config) is R3S3Config
    batches = build_production_gate_batches(
        evidence_context=_EVIDENCE_CONTEXT,
        phase="TRAIN",
        config=config,
        fold_sources=_s3_sources(config),
    )

    assert tuple(batch.fold_id for batch in batches) == tuple(
        f"fold-{index:02d}" for index in range(8)
    )
    assert all(batch.evaluated_decision_units > 3 for batch in batches)
    assert all(
        batch.context_valid_denominator == batch.evaluated_decision_units
        for batch in batches
    )
    first_id = batches[0].units[0].unit_id
    assert json.loads(first_id) == [
        R3_GATE_UNIT_ID_VERSION,
        "S3",
        _fold(0).train_start_ms,
        "XRPUSDT",
    ]
    assert first_id == canonical_gate_unit_id(
        family="S3",
        decision_ts=_fold(0).train_start_ms,
        symbol_or_pair="XRPUSDT",
    )
    assert batches[0].units[0].market_direction == "long"
    assert "market_direction" not in dict(batches[0].units[0].gate_results)


def test_s4_finite_phi_outside_is_context_valid_and_all_atoms_are_retained() -> None:
    config = get_r3_config("S4-R3-00")
    assert type(config) is R3S4Config
    batches = build_production_gate_batches(
        evidence_context=_EVIDENCE_CONTEXT,
        phase="TRAIN",
        config=config,
        fold_sources=_s4_sources(config),
    )

    first = batches[0]
    assert first.evaluated_decision_units > 3
    assert first.context_valid_denominator == first.evaluated_decision_units - 1
    assert first.required_context_failures == 1
    gates = dict(first.units[0].gate_results)
    assert gates["phi_open_unit_interval"] is False
    assert gates["half_life"] is False
    assert gates["convergence_sign"] is True
    assert gates["prior_z_magnitude"] is True
    assert gates["current_z_magnitude"] is True
    assert gates["rho"] is True
    assert gates["notional_feasibility"] is True
    assert len(gates) == 11


def test_exact_eight_fold_hash_and_phase_authority_fail_closed() -> None:
    config = get_r3_config("S3-R3-02")
    assert type(config) is R3S3Config
    sources = _s3_sources(config)
    train = build_production_gate_audit(
        evidence_context=_EVIDENCE_CONTEXT,
        phase="TRAIN",
        config=config,
        fold_sources=sources,
    )
    assert train.threshold_authority is True
    assert train.diagnostic_only is False

    with pytest.raises(GateAuditValidationError, match="exactly eight"):
        build_production_gate_batches(
            evidence_context=_EVIDENCE_CONTEXT,
            phase="TRAIN",
            config=config,
            fold_sources=sources[:-1],
        )
    with pytest.raises(R3ProductionEvidenceContextError, match="campaign identity"):
        dataclasses.replace(
            _EVIDENCE_CONTEXT,
            campaign_identity_sha256="c" * 64,
        )
    reordered = (sources[1], sources[0], *sources[2:])
    with pytest.raises(GateAuditValidationError, match="exact H4 authority"):
        build_production_gate_batches(
            evidence_context=_EVIDENCE_CONTEXT,
            phase="TRAIN",
            config=config,
            fold_sources=reordered,
        )


def test_oos_flags_and_source_grid_order_are_not_caller_selected() -> None:
    config = get_r3_config("S3-R3-02")
    assert type(config) is R3S3Config
    sources = _s3_sources(config, "OOS")
    report = build_production_gate_audit(
        evidence_context=_EVIDENCE_CONTEXT,
        phase="OOS",
        config=config,
        fold_sources=sources,
    )
    assert report.diagnostic_only is True
    assert report.threshold_authority is False

    malformed = dataclasses.replace(sources[0], units=tuple(reversed(sources[0].units)))
    with pytest.raises(GateAuditValidationError, match="reordered source grid"):
        build_production_gate_batches(
            evidence_context=_EVIDENCE_CONTEXT,
            phase="OOS",
            config=config,
            fold_sources=(malformed, *sources[1:]),
        )

    boundary_mutant = dataclasses.replace(
        sources[0],
        fold=dataclasses.replace(
            sources[0].fold,
            train_start_ms=sources[0].fold.train_start_ms + FOUR_HOUR_MS,
        ),
    )
    with pytest.raises(GateAuditValidationError, match="exact H4 authority"):
        build_production_gate_batches(
            evidence_context=_EVIDENCE_CONTEXT,
            phase="OOS",
            config=config,
            fold_sources=(boundary_mutant, *sources[1:]),
        )


def test_phi_half_life_and_context_taxonomy_mutants_fail_closed() -> None:
    config = get_r3_config("S4-R3-00")
    assert type(config) is R3S4Config
    decision_ts = _fold(0).train_start_ms
    pair = s4.PAIR_ORDER[0]
    with pytest.raises(ValueError, match="open-unit phi requires finite half-life"):
        dataclasses.replace(
            _s4_observation(config.config_id, decision_ts, pair),
            half_life_4h_bars=None,
        )
    with pytest.raises(ValueError, match="has no half-life"):
        dataclasses.replace(
            _s4_observation(config.config_id, decision_ts, pair, phi=1.10),
            half_life_4h_bars=2.0,
        )
    with pytest.raises(ValueError, match="closed taxonomy"):
        R3S4ObservationOutcome(
            decision_ts,
            pair,
            None,
            "phi_not_in_open_unit_interval",
        )
