"""Adversarial production identity/coverage tests for ROB-974 R3 evidence."""

from __future__ import annotations

import dataclasses

import pytest
import rob974_r3_evidence_context as evidence_context_module
from rob974_r3_evidence_context import (
    R3ProductionEvidenceContextError,
    issue_r3_production_evidence_context,
    require_r3_production_evidence_context,
)
from rob974_r3_gate_adapter import (
    ProductionGateEvidenceError,
    build_production_gate_campaign_evidence,
    production_gate_audit_scope,
)
from rob974_r3_gate_metrics import (
    S3_GATE_SCHEMA,
    S4_GATE_SCHEMA,
    ContextValidDecisionUnit,
    GateAuditBatch,
    build_gate_audit,
)
from rob974_r3_manifest import FROZEN_R3_ROSTER, R3S3Config
from rob974_r3_plan import build_production_r3_plan


def _context():
    return issue_r3_production_evidence_context(build_production_r3_plan())


def _report(context, config, phase):
    scope = production_gate_audit_scope(
        evidence_context=context,
        config=config,
        phase=phase,
    )
    schema = S3_GATE_SCHEMA if type(config) is R3S3Config else S4_GATE_SCHEMA
    direction = "long" if type(config) is R3S3Config else None
    batches = tuple(
        GateAuditBatch(
            scope=scope,
            fold_id=fold.fold_id,
            gate_schema=schema,
            evaluated_decision_units=1,
            context_valid_denominator=1,
            required_context_failures=0,
            units=(
                ContextValidDecisionUnit(
                    unit_id=(
                        f"rob974-r3-production-shaped:{config.config_id}:"
                        f"{phase}:{fold.fold_id}"
                    ),
                    gate_results=tuple((gate.name, True) for gate in schema.gates),
                    market_direction=direction,
                ),
            ),
        )
        for fold in context.folds
    )
    return build_gate_audit(expected_scope=scope, batches=batches)


def _reports(context):
    return tuple(
        _report(context, config, phase)
        for config in FROZEN_R3_ROSTER
        for phase in ("TRAIN", "OOS")
    )


def test_plan_issued_context_seals_real_campaign_mapping_folds_and_phases() -> None:
    plan = build_production_r3_plan()
    context = issue_r3_production_evidence_context(plan)

    assert context.campaign_identity_sha256 == plan.full_campaign_hash
    assert context.campaign_run_id == plan.campaign_run_id
    assert context.exact_12_mapping_hash == plan.exact_12_mapping_hash
    assert context.ordered_mapping == plan.ordered_mapping
    assert context.folds == plan.folds
    assert context.phases == ("TRAIN", "OOS")
    assert context.operational_status == "COMPLETE"
    assert context.operational_blocker_reason is None
    assert context.operational_blocker_reason == plan.operational_blocker_reason

    with pytest.raises(R3ProductionEvidenceContextError, match="campaign identity"):
        dataclasses.replace(context, campaign_identity_sha256="f" * 64)
    with pytest.raises(R3ProductionEvidenceContextError, match="mapping hash"):
        dataclasses.replace(context, exact_12_mapping_hash="e" * 64)
    with pytest.raises(R3ProductionEvidenceContextError, match="ordered mapping"):
        dataclasses.replace(
            context,
            ordered_mapping=(
                (context.ordered_mapping[0][0], "d" * 64),
                *context.ordered_mapping[1:],
            ),
        )


def test_issued_context_is_rechecked_against_a_fresh_canonical_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    monkeypatch.setattr(
        evidence_context_module,
        "build_production_r3_plan",
        lambda: object(),
    )
    with pytest.raises(R3ProductionEvidenceContextError, match="freshly derived"):
        require_r3_production_evidence_context(context)


def test_exact_24_report_192_cell_envelope_is_promoted_by_ready_plan() -> None:
    context = _context()
    reports = _reports(context)
    envelope = build_production_gate_campaign_evidence(
        evidence_context=context,
        reports=reports,
    )

    expected_keys = tuple(
        (config.config_id, phase, fold.fold_id)
        for config in FROZEN_R3_ROSTER
        for phase in ("TRAIN", "OOS")
        for fold in context.folds
    )
    assert len(envelope.reports) == 12 * 2 == 24
    assert envelope.evidence_cell_order == expected_keys
    assert len(envelope.evidence_cell_order) == 12 * 2 * 8 == 192
    assert envelope.coverage_complete is True
    assert envelope.operational_status == "COMPLETE"
    assert envelope.evidence_promoted is True
    assert envelope.incomplete_reason is None
    assert envelope.campaign_identity_sha256 == context.campaign_identity_sha256
    assert envelope.exact_12_mapping_hash == context.exact_12_mapping_hash
    assert envelope.ordered_mapping == context.ordered_mapping


@pytest.mark.parametrize("mutation", ("23", "25", "duplicate", "reorder"))
def test_envelope_rejects_partial_extra_duplicate_and_reordered_reports(
    mutation: str,
) -> None:
    context = _context()
    reports = _reports(context)
    mutated = {
        "23": reports[:-1],
        "25": (*reports, reports[-1]),
        "duplicate": (reports[0], reports[0], *reports[2:]),
        "reorder": (reports[1], reports[0], *reports[2:]),
    }[mutation]
    with pytest.raises(ProductionGateEvidenceError, match="canonical 12x2"):
        build_production_gate_campaign_evidence(
            evidence_context=context,
            reports=mutated,
        )


def test_envelope_rejects_valid_looking_alternate_identities_and_fold_drift() -> None:
    context = _context()
    reports = _reports(context)

    bad_campaign_scope = dataclasses.replace(
        reports[0].scope,
        campaign_identity_sha256="f" * 64,
    )
    bad_campaign = dataclasses.replace(reports[0], scope=bad_campaign_scope)
    with pytest.raises(ProductionGateEvidenceError, match="issued scope"):
        build_production_gate_campaign_evidence(
            evidence_context=context,
            reports=(bad_campaign, *reports[1:]),
        )

    bad_experiment_scope = dataclasses.replace(
        reports[0].scope,
        experiment_identity_sha256="e" * 64,
    )
    bad_experiment = dataclasses.replace(reports[0], scope=bad_experiment_scope)
    with pytest.raises(ProductionGateEvidenceError, match="issued scope"):
        build_production_gate_campaign_evidence(
            evidence_context=context,
            reports=(bad_experiment, *reports[1:]),
        )

    fold_drift = dataclasses.replace(
        reports[0],
        folds=(reports[0].folds[1], reports[0].folds[0], *reports[0].folds[2:]),
    )
    with pytest.raises(ProductionGateEvidenceError, match="exact eight-fold"):
        build_production_gate_campaign_evidence(
            evidence_context=context,
            reports=(fold_drift, *reports[1:]),
        )
