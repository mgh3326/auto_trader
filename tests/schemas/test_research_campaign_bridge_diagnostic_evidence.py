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


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "truncated": False,
            "omitted_distinct_signatures": 0,
            "omitted_occurrences": 1,
        },
        {"truncated": True, "omitted_distinct_signatures": 0, "omitted_occurrences": 0},
        {
            "truncated": False,
            "omitted_distinct_signatures": 1,
            "omitted_occurrences": 1,
        },
    ],
)
def test_diagnostic_overflow_rejects_inconsistent_truncated_flag(overrides):
    """R2 audit: truncated must be exactly (omitted_occurrences > 0) -- never
    a caller-asserted boolean independent of the actual counts."""
    with pytest.raises(ValidationError):
        _overflow(**overrides)


def test_attempt_evidence_rejects_diagnostic_evidence_longer_than_the_cap():
    """R2 audit: the app schema itself must independently fail closed if
    len(diagnostic_evidence) > 32, not only the producer helper."""
    too_many = tuple(_diagnostic(signature=("a" * 63) + str(i)) for i in range(33))
    with pytest.raises(ValidationError):
        AttemptEvidence(
            attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
            status="crashed",
            reason_code="child_execution_crashed",
            run_identity="r" * 64,
            scenario_evidence=_scenario_evidence(),
            diagnostic_evidence=too_many,
        )


def test_attempt_evidence_accepts_diagnostic_evidence_exactly_at_the_cap():
    exactly_32 = tuple(_diagnostic(signature=("a" * 63) + str(i)) for i in range(32))
    evidence = AttemptEvidence(
        attempt_key=AttemptKey(campaign_run_id="run-1", experiment_id="exp-1"),
        status="crashed",
        reason_code="child_execution_crashed",
        run_identity="r" * 64,
        scenario_evidence=_scenario_evidence(),
        diagnostic_evidence=exactly_32,
    )
    assert len(evidence.diagnostic_evidence) == 32


# -- R2 Critical: fail-closed persistence-boundary safety on HOSTILE direct
# schema construction (never assume the caller already sanitized) ---------


@pytest.mark.parametrize(
    "unsafe_field,unsafe_value",
    [
        ("message", "unquoted secret_key=sk-RAWSECRET123abc"),
        ("message", "quoted {'OPENAI_API_KEY': 'sk-live-RAWSECRET123'}"),
        ("message", "env dict {'AWS_SECRET_ACCESS_KEY': 'AKIA-FAKE', 'X': 'y'}"),
        ("message", "dsn=postgresql://user:hunter2@localhost:5432/prod"),
        (
            "message",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.rawpart",
        ),
        ("message", "/Users/mgh3326/work/auto_trader.rob-970/.env leaked SECRET=abc"),
        (
            "message",
            "raw row TradeRecord(entry_ts=1,symbol='BTCUSDT',net_bps=1.0,x=2)",
        ),
        ("traceback_text", "Traceback...\n  api_key=sk-RAWSECRET456\nValueError: x\n"),
    ],
)
def test_child_failure_diagnostic_rejects_hostile_direct_construction_with_unsafe_content(
    unsafe_field, unsafe_value
):
    """R2 Critical: a directly constructed ChildFailureDiagnostic (bypassing
    the sanitized research capture path entirely) must still fail closed if
    message/traceback_text carries secret/env-dump/DSN/JWT/path/raw-record
    content -- the schema itself is the last trust boundary before H6
    raw_payload persistence, never merely trusting the caller already
    sanitized."""
    with pytest.raises(ValidationError):
        _diagnostic(**{unsafe_field: unsafe_value})


def test_child_failure_diagnostic_accepts_genuinely_safe_content():
    """The safety validator must not misfire on ordinary safe content --
    this is the legitimate research-capture path's normal shape."""
    diag = _diagnostic(
        message="duplicate/colliding rejection signal_ts for S2/S2-00/BTCUSDT/fold-00",
        traceback_text='Traceback (most recent call last):\n  File "rob944_walkforward.py", line 998, in _train_evidence_for_symbol\nValueError: boom\n',
    )
    assert diag.message
    assert diag.traceback_text
