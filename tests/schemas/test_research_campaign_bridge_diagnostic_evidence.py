"""ROB-970 (Q2/Q3, Fable-approved orch-fable-answer-rob970-20260719.md) --
``AttemptEvidence.diagnostic_evidence`` schema: additive, sanitized,
persistence-only child-failure evidence, never a hash/identity input.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.research_campaign_bridge import (
    AttemptEvidence,
    AttemptKey,
    ChildFailureDiagnostic,
    ChildFailureDiagnosticOverflow,
    ScenarioEvidence,
)


def _scenario_evidence() -> tuple[ScenarioEvidence, ScenarioEvidence, ScenarioEvidence]:
    return (
        ScenarioEvidence(scenario_name="base", trade_count=0, artifact_hash="a" * 64),
        ScenarioEvidence(
            scenario_name="primary_stress", trade_count=0, artifact_hash="b" * 64
        ),
        ScenarioEvidence(
            scenario_name="upward_stress", trade_count=0, artifact_hash="c" * 64
        ),
    )


def _diagnostic(**overrides) -> ChildFailureDiagnostic:
    base = {
        "transport": "in_process",
        "stage": "generator",
        "exception_type": "RuntimeError",
        "message": "boom",
        "traceback_text": "Traceback...\nRuntimeError: boom\n",
        "stderr": None,
        "strategy": "S1",
        "config_id": "S1-00",
        "symbol": "BTCUSDT",
        "fold_id": "fold-00",
        "scenario_name": None,
        "signature": "a" * 64,
        "occurrence_count": 1,
        "truncated": False,
    }
    base.update(overrides)
    return ChildFailureDiagnostic(**base)


def test_attempt_evidence_defaults_diagnostic_evidence_to_empty_tuple():
    evidence = AttemptEvidence(
        attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
        status="completed",
        run_identity="r" * 64,
        scenario_evidence=_scenario_evidence(),
    )
    assert evidence.diagnostic_evidence == ()


def test_attempt_evidence_accepts_diagnostic_evidence_entries():
    diag = _diagnostic()
    evidence = AttemptEvidence(
        attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
        status="crashed",
        reason_code="child_execution_crashed",
        run_identity="r" * 64,
        scenario_evidence=_scenario_evidence(),
        diagnostic_evidence=(diag,),
    )
    assert evidence.diagnostic_evidence == (diag,)


def test_child_failure_diagnostic_rejects_unknown_transport():
    with pytest.raises(ValidationError):
        _diagnostic(transport="subprocess")


def test_child_failure_diagnostic_rejects_unknown_stage():
    with pytest.raises(ValidationError):
        _diagnostic(stage="database")


def test_child_failure_diagnostic_rejects_in_process_with_fabricated_stderr():
    with pytest.raises(ValidationError):
        _diagnostic(stderr="fabricated stderr text")


def test_child_failure_diagnostic_rejects_occurrence_count_below_one():
    with pytest.raises(ValidationError):
        _diagnostic(occurrence_count=0)


def test_child_failure_diagnostic_rejects_extra_fields():
    with pytest.raises(ValidationError):
        _diagnostic(unexpected_field="nope")


def test_attempt_evidence_still_forbids_extra_top_level_fields():
    with pytest.raises(ValidationError):
        AttemptEvidence(
            attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
            status="completed",
            run_identity="r" * 64,
            scenario_evidence=_scenario_evidence(),
            unexpected_field="nope",
        )


# -- ROB-970 R1 (Q1=A, cap=32): diagnostic_overflow ------------------------


def _overflow(**overrides) -> ChildFailureDiagnosticOverflow:
    base = {
        "truncated": True,
        "omitted_distinct_signatures": 3,
        "omitted_occurrences": 9,
    }
    base.update(overrides)
    return ChildFailureDiagnosticOverflow(**base)


def test_attempt_evidence_defaults_diagnostic_overflow_to_empty():
    evidence = AttemptEvidence(
        attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
        status="completed",
        run_identity="r" * 64,
        scenario_evidence=_scenario_evidence(),
    )
    assert evidence.diagnostic_overflow == ChildFailureDiagnosticOverflow(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


def test_attempt_evidence_accepts_diagnostic_overflow():
    overflow = _overflow()
    evidence = AttemptEvidence(
        attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
        status="crashed",
        reason_code="child_execution_crashed",
        run_identity="r" * 64,
        scenario_evidence=_scenario_evidence(),
        diagnostic_overflow=overflow,
    )
    assert evidence.diagnostic_overflow == overflow


def test_diagnostic_overflow_rejects_negative_omitted_distinct_signatures():
    with pytest.raises(ValidationError):
        _overflow(omitted_distinct_signatures=-1)


def test_diagnostic_overflow_rejects_negative_omitted_occurrences():
    with pytest.raises(ValidationError):
        _overflow(omitted_occurrences=-1)


def test_diagnostic_overflow_rejects_distinct_exceeding_occurrences():
    with pytest.raises(ValidationError):
        _overflow(omitted_distinct_signatures=10, omitted_occurrences=5)


def test_diagnostic_overflow_rejects_extra_fields():
    with pytest.raises(ValidationError):
        _overflow(unexpected_field="nope")
