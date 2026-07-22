from __future__ import annotations

import dataclasses
import hashlib
import math

import pytest
from rob974_r3_evidence_context import issue_r3_production_evidence_context
from rob974_r3_gate_metrics import (
    R3_CONFIG_IDS,
    S3_GATE_SCHEMA,
    S4_GATE_SCHEMA,
    AuditScope,
    ContextValidDecisionUnit,
    GateAuditBatch,
    GateAuditValidationError,
    PairwiseTable,
    build_gate_audit,
    validate_gate_audit,
)
from rob974_r3_plan import build_production_r3_plan

from research_contracts.canonical_hash import canonical_json

EVIDENCE_CONTEXT = issue_r3_production_evidence_context(build_production_r3_plan())
CAMPAIGN_SHA256 = EVIDENCE_CONTEXT.campaign_identity_sha256
EXPERIMENT_BY_CONFIG = dict(EVIDENCE_CONTEXT.ordered_mapping)
EXPERIMENT_SHA256 = EXPERIMENT_BY_CONFIG["S3-R3-02"]
CONFIG_SHA256 = "7b752d2b902e0176aa92855ac3ae96e7d87f42bb9fc2db968af7516eed35ef81"

R3_CONFIG_IDENTITY_PAYLOADS: tuple[dict[str, object], ...] = tuple(
    {
        "schema_version": "rob974-r3-config-identity-v1",
        "family": "S3",
        "config_id": f"S3-R3-{index:02d}",
        "parameters": {
            "L": 16,
            "q_min": 0.35,
            "ER_min": 0.35,
            "k_SL": 1.25,
            "R_TP": 1.60,
            "S_min": s_min,
            "M_min_bp": m_min_bp,
        },
    }
    for index, (s_min, m_min_bp) in enumerate(((0.05, 0), (0.0, 25), (0.0, 0)))
) + tuple(
    {
        "schema_version": "rob974-r3-config-identity-v1",
        "family": "S4",
        "config_id": f"S4-R3-{index:02d}",
        "parameters": {
            "W": 150,
            "k_SL": 1.25,
            "R_TP": 1.50,
            "z_entry": z_entry,
            "d_min_bp": d_min_bp,
        },
    }
    for index, (z_entry, d_min_bp) in enumerate(
        (
            (1.10, 140),
            (1.00, 160),
            (1.00, 140),
            (0.80, 180),
            (0.80, 160),
            (0.80, 140),
            (0.60, 180),
            (0.60, 160),
            (0.60, 140),
        )
    )
)


def _scope(
    *,
    phase: str = "TRAIN",
    family: str = "S3",
    config_id: str = "S3-R3-02",
    config_sha256: str = CONFIG_SHA256,
    experiment_sha256: str = EXPERIMENT_SHA256,
) -> AuditScope:
    return AuditScope(
        phase=phase,
        family=family,
        config_id=config_id,
        campaign_identity_sha256=CAMPAIGN_SHA256,
        experiment_identity_sha256=experiment_sha256,
        config_identity_sha256=config_sha256,
    )


def _unit(
    unit_id: str,
    schema=S3_GATE_SCHEMA,
    *,
    market_direction: str | None = None,
    **overrides: bool,
):
    unknown = set(overrides) - {gate.name for gate in schema.gates}
    if unknown:
        raise ValueError(f"unknown fixture gate override: {sorted(unknown)}")
    values = tuple((gate.name, overrides.get(gate.name, True)) for gate in schema.gates)
    if schema.family == "S3" and market_direction is None:
        market_direction = "long"
    return ContextValidDecisionUnit(
        unit_id=unit_id,
        gate_results=values,
        market_direction=market_direction,
    )


def _batch(
    fold_id: str,
    units: tuple[ContextValidDecisionUnit, ...],
    *,
    scope: AuditScope | None = None,
    required_context_failures: int = 0,
    schema=S3_GATE_SCHEMA,
) -> GateAuditBatch:
    return GateAuditBatch(
        scope=scope or _scope(),
        fold_id=fold_id,
        gate_schema=schema,
        evaluated_decision_units=len(units) + required_context_failures,
        context_valid_denominator=len(units),
        required_context_failures=required_context_failures,
        units=units,
    )


def _two_fold_fixture() -> tuple[GateAuditBatch, GateAuditBatch]:
    fold_00 = _batch(
        "fold-00",
        (
            _unit("2025-10-29T03:00:00Z:XRPUSDT:long"),
            _unit(
                "2025-10-29T07:00:00Z:DOGEUSDT:long",
                market_direction="short",
                market_magnitude=False,
            ),
            _unit(
                "2025-10-29T11:00:00Z:SOLUSDT:long",
                market_breadth=False,
            ),
        ),
        required_context_failures=1,
    )
    fold_01 = _batch(
        "fold-01",
        (
            _unit(
                "2025-11-26T03:00:00Z:XRPUSDT:short",
                market_direction="short",
            ),
        ),
    )
    return fold_00, fold_01


def _named(items, name: str):
    return next(item for item in items if item.name == name)


def _pair(metric, first: str, second: str):
    return next(
        item
        for item in metric.pairwise
        if item.first_gate == first and item.second_gate == second
    )


def test_fold_and_pooled_metrics_preserve_raw_counts_and_sum_pooling() -> None:
    batches = _two_fold_fixture()
    report = build_gate_audit(expected_scope=_scope(), batches=batches)

    assert report.schema_version == "rob974-r3-gate-audit-v1"
    assert report.diagnostic_only is False
    assert report.threshold_authority is True
    assert [item.fold_id for item in report.folds] == ["fold-00", "fold-01"]

    fold = report.folds[0]
    assert fold.evaluated_decision_units == 4
    assert fold.context_valid_denominator == 3
    assert fold.required_context_failures == 1
    assert fold.required_context_rate.as_tuple() == (3, 4, 0.75, None)
    assert fold.s3_market_direction is not None
    assert fold.s3_market_direction.long_rate.as_tuple() == (2, 3, 2 / 3, None)
    assert fold.s3_market_direction.short_rate.as_tuple() == (1, 3, 1 / 3, None)
    assert fold.joint_pass_rate.as_tuple() == (1, 3, 1 / 3, None)
    assert _named(fold.single_gate_pass_rates, "market_magnitude").rate.as_tuple() == (
        2,
        3,
        2 / 3,
        None,
    )
    assert _named(
        fold.sequential_conditional_pass_rates, "market_breadth"
    ).rate.as_tuple() == (1, 2, 0.5, None)
    assert _named(
        fold.leave_one_gate_out_rates, "market_magnitude"
    ).rate.as_tuple() == (2, 3, 2 / 3, None)
    assert _named(fold.dominant_removed_rates, "M").rate.as_tuple() == (
        3,
        3,
        1.0,
        None,
    )
    assert _named(fold.dominant_removed_rates, "S").rate.as_tuple() == (
        1,
        3,
        1 / 3,
        None,
    )
    assert fold.kappa.joint_rate.as_tuple() == (1, 3, 1 / 3, None)
    assert fold.kappa.single_rate_product.value == pytest.approx(4 / 9)
    assert fold.kappa.kappa.value == pytest.approx(0.75)

    pair = _pair(fold, "market_magnitude", "market_breadth")
    assert len(fold.pairwise) == math.comb(len(S3_GATE_SCHEMA.gates), 2)
    assert (pair.n00, pair.n01, pair.n10, pair.n11, pair.denominator) == (
        0,
        1,
        1,
        1,
        3,
    )
    assert pair.rates.n11.as_tuple() == (1, 3, 1 / 3, None)

    pooled = report.pooled
    assert pooled.context_valid_denominator == 4
    assert pooled.required_context_failures == 1
    assert pooled.s3_market_direction is not None
    assert pooled.s3_market_direction.long_rate.as_tuple() == (2, 4, 0.5, None)
    assert pooled.s3_market_direction.short_rate.as_tuple() == (2, 4, 0.5, None)
    assert pooled.joint_pass_rate.as_tuple() == (2, 4, 0.5, None)
    pooled_single = _named(pooled.single_gate_pass_rates, "market_magnitude").rate
    assert pooled_single.as_tuple() == (3, 4, 0.75, None)
    assert pooled_single.value != pytest.approx(((2 / 3) + 1.0) / 2)
    rate_range = _named(report.fold_rate_ranges, "single:market_magnitude")
    assert rate_range.minimum == pytest.approx(2 / 3)
    assert rate_range.maximum == 1.0
    assert rate_range.reason is None
    direction_range = _named(report.fold_rate_ranges, "market_direction:long")
    assert direction_range.minimum == 0.0
    assert direction_range.maximum == pytest.approx(2 / 3)

    validate_gate_audit(report=report, batches=batches)


def test_oos_is_diagnostic_only_and_has_no_threshold_authority() -> None:
    scope = _scope(phase="OOS")
    batch = _batch(
        "fold-00",
        (_unit("2025-10-29T03:00:00Z:XRPUSDT:long"),),
        scope=scope,
    )
    report = build_gate_audit(expected_scope=scope, batches=(batch,))

    assert report.diagnostic_only is True
    assert report.threshold_authority is False
    assert report.scope.config_identity_sha256 == CONFIG_SHA256


def test_zero_denominator_and_zero_product_are_null_with_closed_reasons() -> None:
    empty = _batch("fold-00", (), required_context_failures=2)
    empty_report = build_gate_audit(expected_scope=_scope(), batches=(empty,))
    assert empty_report.folds[0].joint_pass_rate.as_tuple() == (
        0,
        0,
        None,
        "zero_denominator",
    )
    assert empty_report.folds[0].s3_market_direction is not None
    assert empty_report.folds[0].s3_market_direction.long_rate.as_tuple() == (
        0,
        0,
        None,
        "zero_denominator",
    )
    assert empty_report.folds[0].s3_market_direction.short_rate.as_tuple() == (
        0,
        0,
        None,
        "zero_denominator",
    )
    assert empty_report.folds[0].kappa.single_rate_product.as_tuple() == (
        None,
        "zero_denominator",
    )
    assert empty_report.folds[0].kappa.kappa.as_tuple() == (
        None,
        "zero_denominator",
    )

    zero_single = _batch(
        "fold-00",
        (
            _unit("unit-1", market_magnitude=False),
            _unit("unit-2", market_magnitude=False),
        ),
    )
    report = build_gate_audit(expected_scope=_scope(), batches=(zero_single,))
    assert report.folds[0].kappa.single_rate_product.as_tuple() == (0.0, None)
    assert report.folds[0].kappa.kappa.as_tuple() == (
        None,
        "zero_single_rate_product",
    )
    assert not any(
        math.isnan(value) or math.isinf(value)
        for value in (
            item.rate.value
            for item in report.folds[0].single_gate_pass_rates
            if item.rate.value is not None
        )
    )


def test_schema_covers_frozen_s3_and_s4_atomic_decomposition() -> None:
    assert tuple(item.name for item in S3_GATE_SCHEMA.gates) == (
        "market_magnitude",
        "market_breadth",
        "trend_sign_alignment",
        "trend_magnitude",
        "efficiency_ratio",
        "pullback_depth",
        "vwap_reclaim",
        "momentum",
        "prior_l_non_breakout",
        "volatility_percentile",
        "range_to_tp_capacity",
    )
    assert tuple(item.name for item in S4_GATE_SCHEMA.gates) == (
        # Context-valid S4 estimation tests phi before evaluate_s4_gates;
        # everything after it is the generator's exact first-fail order.
        "phi_open_unit_interval",
        "convergence_sign",
        "prior_z_magnitude",
        "current_z_magnitude",
        "convergence_fraction",
        "rho",
        "half_life",
        "beta_stability",
        "d_min_distance",
        "distance_to_tp",
        "notional_feasibility",
    )
    assert tuple(item.name for item in S3_GATE_SCHEMA.dominant_removals) == (
        "S",
        "M",
        "S+M",
    )
    assert S3_GATE_SCHEMA.dominant_removals[1].gate_names == (
        "market_magnitude",
        "market_breadth",
    )
    assert S3_GATE_SCHEMA.dominant_removals[2].gate_names == (
        "market_magnitude",
        "market_breadth",
        "trend_sign_alignment",
        "trend_magnitude",
    )
    assert tuple(item.name for item in S4_GATE_SCHEMA.dominant_removals) == (
        "prior_current_z_magnitude",
        "d_min",
        "z+d",
    )
    assert len(S4_GATE_SCHEMA.gates) == 11


def test_s3_dominant_m_removes_breadth_but_dominant_s_retains_it() -> None:
    batch = _batch(
        "fold-00",
        (
            _unit("all-pass"),
            _unit("breadth-false", market_breadth=False),
        ),
    )
    fold = build_gate_audit(expected_scope=_scope(), batches=(batch,)).folds[0]

    assert _named(fold.dominant_removed_rates, "M").rate.as_tuple() == (
        2,
        2,
        1.0,
        None,
    )
    assert _named(fold.dominant_removed_rates, "S").rate.as_tuple() == (
        1,
        2,
        0.5,
        None,
    )


def test_s3_market_direction_is_categorical_and_never_enters_gate_arithmetic() -> None:
    long_batch = _batch("fold-00", (_unit("unit", market_direction="long"),))
    short_batch = _batch("fold-00", (_unit("unit", market_direction="short"),))
    long_report = build_gate_audit(expected_scope=_scope(), batches=(long_batch,))
    short_report = build_gate_audit(expected_scope=_scope(), batches=(short_batch,))

    long_fold = long_report.folds[0]
    short_fold = short_report.folds[0]
    assert long_fold.s3_market_direction != short_fold.s3_market_direction
    assert long_fold.joint_pass_rate == short_fold.joint_pass_rate
    assert long_fold.single_gate_pass_rates == short_fold.single_gate_pass_rates
    assert (
        long_fold.sequential_conditional_pass_rates
        == short_fold.sequential_conditional_pass_rates
    )
    assert long_fold.leave_one_gate_out_rates == short_fold.leave_one_gate_out_rates
    assert long_fold.dominant_removed_rates == short_fold.dominant_removed_rates
    assert long_fold.kappa == short_fold.kappa
    assert long_fold.pairwise == short_fold.pairwise
    assert "market_direction" not in tuple(gate.name for gate in S3_GATE_SCHEMA.gates)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda report: dataclasses.replace(
                report,
                folds=(
                    dataclasses.replace(
                        report.folds[0],
                        leave_one_gate_out_rates=(
                            dataclasses.replace(
                                report.folds[0].leave_one_gate_out_rates[0],
                                rate=dataclasses.replace(
                                    report.folds[0].leave_one_gate_out_rates[0].rate,
                                    numerator=1,
                                    value=1 / 3,
                                ),
                            ),
                            *report.folds[0].leave_one_gate_out_rates[1:],
                        ),
                    ),
                    *report.folds[1:],
                ),
            ),
            "report does not match atomic unit evidence",
        ),
        (
            lambda report: dataclasses.replace(
                report,
                folds=(
                    dataclasses.replace(
                        report.folds[0],
                        sequential_conditional_pass_rates=(
                            report.folds[0].sequential_conditional_pass_rates[0],
                            dataclasses.replace(
                                report.folds[0].sequential_conditional_pass_rates[1],
                                rate=dataclasses.replace(
                                    report.folds[0]
                                    .sequential_conditional_pass_rates[1]
                                    .rate,
                                    denominator=3,
                                    value=1 / 3,
                                ),
                            ),
                            *report.folds[0].sequential_conditional_pass_rates[2:],
                        ),
                    ),
                    *report.folds[1:],
                ),
            ),
            "report does not match atomic unit evidence",
        ),
        (
            lambda report: dataclasses.replace(
                report,
                pooled=dataclasses.replace(
                    report.pooled,
                    single_gate_pass_rates=(
                        dataclasses.replace(
                            report.pooled.single_gate_pass_rates[0],
                            rate=dataclasses.replace(
                                report.pooled.single_gate_pass_rates[0].rate,
                                numerator=5,
                                denominator=6,
                                value=5 / 6,
                            ),
                        ),
                        *report.pooled.single_gate_pass_rates[1:],
                    ),
                ),
            ),
            "report does not match atomic unit evidence",
        ),
    ],
)
def test_validator_rejects_loo_sequential_and_mean_of_means_mutants(
    mutator, message: str
) -> None:
    batches = _two_fold_fixture()
    report = build_gate_audit(expected_scope=_scope(), batches=batches)
    with pytest.raises(GateAuditValidationError, match=message):
        validate_gate_audit(report=mutator(report), batches=batches)


def test_pairwise_table_rejects_cell_sum_mismatch() -> None:
    with pytest.raises(GateAuditValidationError, match="pairwise cell sum"):
        PairwiseTable.from_counts(
            first_gate="market_magnitude",
            second_gate="market_breadth",
            denominator=3,
            n00=1,
            n01=1,
            n10=1,
            n11=1,
        )


def test_terminal_histogram_cannot_substitute_for_atomic_unit_vectors() -> None:
    with pytest.raises(TypeError, match="exact ContextValidDecisionUnit"):
        GateAuditBatch(
            scope=_scope(),
            fold_id="fold-00",
            gate_schema=S3_GATE_SCHEMA,
            evaluated_decision_units=3,
            context_valid_denominator=3,
            required_context_failures=0,
            units=(("market_regime", 3),),  # type: ignore[arg-type]
        )


def test_fail_closed_on_identity_phase_schema_bool_duplicate_and_denominator_drift() -> (
    None
):
    with pytest.raises(ValueError, match="closed phase"):
        _scope(phase="train")
    with pytest.raises(TypeError, match="phase must be built-in str"):
        _scope(phase=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="family/config mismatch"):
        _scope(family="S4")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _scope(config_sha256="abc")

    values = tuple((gate.name, True) for gate in S3_GATE_SCHEMA.gates)
    with pytest.raises(TypeError, match="built-in bool"):
        ContextValidDecisionUnit(
            "unit-1",
            ((values[0][0], 1), *values[1:]),  # type: ignore[arg-type]
        )

    valid = _unit("unit-1")
    malformed_vectors = (
        dataclasses.replace(valid, gate_results=valid.gate_results[:-1]),
        dataclasses.replace(
            valid,
            gate_results=(("unknown_gate", True), *valid.gate_results[1:]),
        ),
        dataclasses.replace(
            valid,
            gate_results=(valid.gate_results[1], valid.gate_results[0])
            + valid.gate_results[2:],
        ),
    )
    for malformed in malformed_vectors:
        with pytest.raises(GateAuditValidationError, match="keys/order mismatch"):
            _batch("fold-00", (malformed,))

    duplicate = _unit("unit-1")
    with pytest.raises(GateAuditValidationError, match="duplicate decision unit"):
        _batch("fold-00", (duplicate, duplicate))

    with pytest.raises(GateAuditValidationError, match="denominator equation"):
        dataclasses.replace(_batch("fold-00", (duplicate,)), evaluated_decision_units=2)

    train_batch = _batch("fold-00", (duplicate,))
    with pytest.raises(GateAuditValidationError, match="scope drift"):
        build_gate_audit(expected_scope=_scope(phase="OOS"), batches=(train_batch,))
    with pytest.raises(ValueError, match="fold_id"):
        dataclasses.replace(train_batch, fold_id="fold-08")


def test_oos_config_identity_mutation_is_rejected() -> None:
    expected = _scope(phase="OOS")
    mutated = _scope(phase="OOS", config_sha256="e" * 64)
    batch = _batch("fold-00", (_unit("unit-1"),), scope=mutated)
    with pytest.raises(GateAuditValidationError, match="scope drift"):
        build_gate_audit(expected_scope=expected, batches=(batch,))


def test_production_shaped_all_r3_ids_eight_folds_and_full_hashes() -> None:
    assert R3_CONFIG_IDS == tuple(f"S3-R3-{index:02d}" for index in range(3)) + tuple(
        f"S4-R3-{index:02d}" for index in range(9)
    )
    assert tuple(payload["config_id"] for payload in R3_CONFIG_IDENTITY_PAYLOADS) == (
        R3_CONFIG_IDS
    )
    config_hashes = tuple(
        hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        for payload in R3_CONFIG_IDENTITY_PAYLOADS
    )
    assert len(set(config_hashes)) == 12

    for config_id, config_hash in zip(R3_CONFIG_IDS, config_hashes, strict=True):
        family = config_id[:2]
        scope = _scope(
            phase="OOS",
            family=family,
            config_id=config_id,
            config_sha256=config_hash,
            experiment_sha256=EXPERIMENT_BY_CONFIG[config_id],
        )
        schema = S3_GATE_SCHEMA if family == "S3" else S4_GATE_SCHEMA
        batches = tuple(
            _batch(
                f"fold-{fold_index:02d}",
                (
                    _unit(
                        f"{1_751_328_000_000 + fold_index * 2_419_200_000}:"
                        f"{config_id}:decision-000",
                        schema,
                    ),
                ),
                scope=scope,
                schema=schema,
            )
            for fold_index in range(8)
        )
        report = build_gate_audit(expected_scope=scope, batches=batches)
        assert tuple(fold.fold_id for fold in report.folds) == tuple(
            f"fold-{index:02d}" for index in range(8)
        )
        assert len(report.scope.campaign_identity_sha256) == 64
        assert len(report.scope.experiment_identity_sha256) == 64
        assert report.scope.config_identity_sha256 == config_hash
        assert report.diagnostic_only and not report.threshold_authority
