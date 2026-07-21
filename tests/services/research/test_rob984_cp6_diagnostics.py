"""ROB-984 CP6 ROB-970 traceback, security, and observer-effect closure."""

from __future__ import annotations

import ast
import asyncio
import json
import traceback
from dataclasses import asdict, replace

import pytest
import rob944_diagnostic_evidence as rob970
import rob974_h6a_diagnostics as h6a_diagnostics

from app.services import rob974_h6a_bridge as bridge
from app.services import rob974_h6b_materializer as materializer
from research.nautilus_scalping.rob974_h6b_cli import ActualRob970DiagnosticPort
from tests.services.research.test_rob984_cp3_transaction_coordinator import (
    ActualH6AAccountingPort,
    Fixture,
)


def _live_exception(message="leaf failure"):
    try:
        local_token = "LOCAL_TOKEN_MUST_NOT_SURVIVE"
        if local_token:
            raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def _capture_payload(message, *, stage="engine"):
    exc = _live_exception(message)
    evidence = h6a_diagnostics.capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage=stage,
        strategy="S3",
        config_id="S3-00",
    )
    return asdict(evidence)


def _overflow(*, truncated=False, distinct=0, occurrences=0):
    return {
        "truncated": truncated,
        "omitted_distinct_signatures": distinct,
        "omitted_occurrences": occurrences,
    }


def test_cp6_materializer_diagnostic_surface_exists() -> None:
    assert materializer.H6BDiagnosticCapture
    assert materializer.render_safe_materialization_failure


@pytest.mark.parametrize(
    ("boundary", "sanitizer_stage"),
    (
        ("feature", "generator"),
        ("generator", "generator"),
        ("funding_gate", "funding_gate"),
        ("engine", "engine"),
        ("metric", "engine"),
        ("materializer", "engine"),
    ),
)
def test_all_first_catch_boundaries_use_actual_rob970_capture(
    boundary, sanitizer_stage
):
    exc = _live_exception(f"{boundary} leaf")
    captured = ActualRob970DiagnosticPort().capture_live_exception(
        exc,
        catch_boundary=boundary,
        strategy="S3",
        config_id="S3-00",
    )
    assert captured.catch_boundary == boundary
    assert captured.sanitizer_stage == sanitizer_stage
    assert captured.exception_type == "RuntimeError"
    assert captured.innermost_file == "test_rob984_cp6_diagnostics.py"
    assert captured.innermost_function == "_live_exception"
    assert captured.innermost_line > 0
    assert len(captured.signature) == 64


def test_authority_calls_tracebackexception_on_same_live_leaf_without_locals(
    monkeypatch,
):
    calls = []
    original = traceback.TracebackException.from_exception

    def recording(exc, *args, **kwargs):
        calls.append((exc, kwargs.get("capture_locals")))
        return original(exc, *args, **kwargs)

    monkeypatch.setattr(
        traceback.TracebackException, "from_exception", staticmethod(recording)
    )
    exc = _live_exception("same-live-leaf")
    ActualRob970DiagnosticPort().capture_live_exception(
        exc,
        catch_boundary="materializer",
        strategy="H6B",
        config_id="ROB-984",
    )
    assert calls == [(exc, False)]


def test_cause_and_context_markers_survive_without_source_lines_or_locals():
    port = ActualRob970DiagnosticPort()
    try:
        try:
            raise ValueError("cause leaf")
        except ValueError as cause:
            raise RuntimeError("outer leaf") from cause
    except RuntimeError as exc:
        caused = port.capture_live_exception(
            exc,
            catch_boundary="materializer",
            strategy="H6B",
            config_id="ROB-984",
        )
    try:
        try:
            raise KeyError("context leaf")
        except KeyError:
            raise RuntimeError("context outer")
    except RuntimeError as exc:
        contextual = port.capture_live_exception(
            exc,
            catch_boundary="metric",
            strategy="S3",
            config_id="S3-00",
        )
    assert caused.has_cause is True
    assert "direct cause" in caused.traceback_text
    assert contextual.has_context is True
    assert "During handling" in contextual.traceback_text
    for captured in (caused, contextual):
        assert "raise RuntimeError" not in captured.traceback_text
        assert "LOCAL_TOKEN" not in captured.traceback_text
        assert "/Users/" not in captured.traceback_text


@pytest.mark.parametrize("message_length", (499, 500, 501))
def test_message_cap_boundaries_are_exact(message_length):
    captured = ActualRob970DiagnosticPort().capture_live_exception(
        _live_exception("x" * message_length),
        catch_boundary="materializer",
        strategy="H6B",
        config_id="ROB-984",
    )
    assert len(captured.message) <= 500
    if message_length > 500:
        assert captured.message.endswith("...<truncated>...")
    else:
        assert len(captured.message) == message_length


@pytest.mark.parametrize("traceback_length", (3999, 4000, 4001))
def test_traceback_cap_boundaries_reuse_authoritative_tail_bound(traceback_length):
    raw = ("t\n" * (traceback_length // 2)) + ("t" if traceback_length % 2 else "")
    assert len(raw) == traceback_length
    bounded, truncated = rob970._bound_tail_by_line(raw, 4000)
    assert len(bounded) <= 4000
    assert truncated is (traceback_length > 4000)
    if traceback_length <= 4000:
        assert bounded == raw
    else:
        assert bounded.startswith("...<truncated>...")


@pytest.mark.parametrize("distinct_count", (31, 32, 33))
def test_distinct_signature_cap_boundaries_and_honest_overflow(distinct_count):
    evidence = [
        h6a_diagnostics.capture_child_failure_evidence(
            _live_exception(f"distinct-{index}"),
            transport="in_process",
            stage="engine",
            strategy="S3",
            config_id="S3-00",
        )
        for index in range(distinct_count)
    ]
    retained, overflow = h6a_diagnostics.accumulate_diagnostic_evidence(evidence)
    assert len(retained) == min(distinct_count, 32)
    assert overflow.truncated is (distinct_count > 32)
    assert overflow.omitted_distinct_signatures == max(distinct_count - 32, 0)
    assert overflow.omitted_occurrences == max(distinct_count - 32, 0)


@pytest.mark.asyncio
async def test_materializer_top_level_catch_reports_sanitized_leaf_not_see_logs(
    tmp_path,
):
    fixture = Fixture(tmp_path)
    raw_token = "sk-RAW-TOKEN-MUST-NOT-SURVIVE"
    raw_dsn = "postgresql://raw_user:raw_password@private-host/secret_db"
    raw_path = "/Users/private/worktree/secret.py"
    failure_holder = {}

    async def failed_h4(_plan):
        try:
            raw_trade = "TradeRecord(symbol='BTC', token='raw')"
            raise RuntimeError(
                f"token={raw_token} dsn={raw_dsn} path={raw_path} {raw_trade}"
            )
        except RuntimeError as exc:
            failure_holder["leaf"] = exc
            raise

    ports = replace(
        fixture.ports,
        run_h4_attempts_fn=failed_h4,
        diagnostics=ActualRob970DiagnosticPort(),
    )
    outcome = await fixture.run(ports=ports)
    assert outcome.primary_error is failure_holder["leaf"]
    assert outcome.diagnostic_capture is not None
    assert outcome.diagnostic_capture.exception_type == "RuntimeError"
    assert outcome.diagnostic_capture.innermost_function == "failed_h4"
    report = materializer.render_safe_materialization_failure(outcome)
    decoded = report.decode()
    assert "see logs" not in decoded.lower()
    assert "RuntimeError" in decoded
    assert "failed_h4" in decoded
    for forbidden in (raw_token, raw_dsn, raw_path, "TradeRecord", "/Users/"):
        assert forbidden not in decoded
    assert outcome.counters.rollback == outcome.counters.close == 1
    assert outcome.counters.commit == outcome.counters.publish == 0


@pytest.mark.asyncio
async def test_diagnostic_capture_failure_is_secondary_to_primary_result(tmp_path):
    fixture = Fixture(tmp_path)
    primary = RuntimeError("primary semantic-safe leaf")
    capture_failure = RuntimeError("diagnostic sink failed")

    class BrokenCapturePort:
        provenance = "actual_merged_rob970_h6a"

        def capture_live_exception(self, *_args, **_kwargs):
            raise capture_failure

    async def failed_h4(_plan):
        raise primary

    outcome = await fixture.run(
        ports=replace(
            fixture.ports,
            run_h4_attempts_fn=failed_h4,
            diagnostics=BrokenCapturePort(),
        )
    )
    assert outcome.primary_error is primary
    assert outcome.diagnostic_capture is None
    assert outcome.diagnostic_capture_error is capture_failure
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE


def test_diagnostics_do_not_change_attempt_accounting_or_h5_fixture_semantics(tmp_path):
    fixture = Fixture(tmp_path)
    baseline = fixture.attempts
    payload = _capture_payload("diagnostic-only variant")
    variant = (
        replace(
            baseline[0],
            diagnostic_evidence=(payload,),
            diagnostic_overflow=_overflow(truncated=True, distinct=1, occurrences=2),
        ),
        *baseline[1:],
    )
    assert variant[0].fingerprint() == baseline[0].fingerprint()
    assert (
        variant[0].status,
        variant[0].reason_code,
        variant[0].fold_evidence_hash,
        variant[0].run_identity,
    ) == (
        baseline[0].status,
        baseline[0].reason_code,
        baseline[0].fold_evidence_hash,
        baseline[0].run_identity,
    )
    assert fixture.plan._fixture_campaign_hash
    assert fixture.plan._fixture_run_id
    accounting = ActualH6AAccountingPort()
    baseline_report = accounting.reconstruct(
        plan=fixture.plan, registered_total=48, attempts=baseline
    )
    variant_report = accounting.reconstruct(
        plan=fixture.plan, registered_total=48, attempts=variant
    )
    assert baseline_report == variant_report
    baseline_scorecard = fixture.h5.build_scorecard(
        plan=fixture.plan, attempts=baseline, accounting=baseline_report
    )
    variant_scorecard = fixture.h5.build_scorecard(
        plan=fixture.plan, attempts=variant, accounting=variant_report
    )
    assert baseline_scorecard == variant_scorecard
    assert fixture.h5.canonical_json_bytes(baseline_scorecard) == (
        fixture.h5.canonical_json_bytes(variant_scorecard)
    )
    assert fixture.h5.semantic_hash(baseline_scorecard) == (
        fixture.h5.semantic_hash(variant_scorecard)
    )
    assert fixture.h5.render_markdown(baseline_scorecard) == (
        fixture.h5.render_markdown(variant_scorecard)
    )


class _ExistingRow:
    def __init__(self, raw_payload, row_id):
        self.raw_payload = raw_payload
        self.id = row_id


async def _record_semantic_replay(
    fixture,
    *,
    target_incoming,
    target_stored_evidence,
    target_stored_overflow,
):
    _register, record_context = materializer.build_h6a_mutation_contexts(
        fixture.authorize()
    )
    attempts = (target_incoming, *fixture.attempts[1:])
    rows = {}
    for index, item in enumerate(attempts, start=1):
        if index == 1:
            evidence = target_stored_evidence
            overflow = target_stored_overflow
        else:
            evidence = item.diagnostic_evidence_payload()
            overflow = item.diagnostic_overflow_payload()
        rows[index] = _ExistingRow(
            {
                "h6a_evidence_fingerprint": item.fingerprint(),
                "diagnostic_evidence": evidence,
                "diagnostic_overflow": overflow,
            },
            index,
        )

    async def find_existing(_session, *, experiment_pk, idempotency_key):
        del idempotency_key
        return rows[experiment_pk]

    record_calls = []

    async def forbidden_record(*args, **kwargs):
        record_calls.append((args, kwargs))
        raise AssertionError("semantic replay attempted a write")

    results = await bridge.record_h6a_attempts(
        object(),
        approved=record_context,
        full_campaign_hash=fixture.plan._fixture_campaign_hash,
        campaign_run_id=fixture.plan._fixture_run_id,
        row_id_to_experiment_id=dict(fixture.plan.ordered_mapping),
        row_id_to_experiment_pk={
            row_id: index
            for index, (row_id, _experiment_id) in enumerate(
                fixture.plan.ordered_mapping, start=1
            )
        },
        attempts=attempts,
        strategy_name="rob974-h6b-contract-fixture",
        timeframe="4h",
        runner="rob984-cp6",
        guard_opt_in_enabled=True,
        guard_policy=fixture.campaign.guard_policy,
        find_existing_trial_fn=find_existing,
        record_trial_fn=forbidden_record,
    )
    assert record_calls == []
    return results, rows


@pytest.mark.parametrize(
    "variant",
    (
        "identical",
        "absent_present",
        "reworded",
        "reordered",
        "overflow",
        "malformed",
    ),
)
@pytest.mark.asyncio
async def test_actual_h6a_service_replay_is_observer_effect_zero(
    tmp_path, capsys, variant
):
    fixture = Fixture(tmp_path)
    baseline = fixture.attempts[0]
    diagnostic = _capture_payload("baseline diagnostic")
    incoming_diagnostic = diagnostic
    incoming_evidence = (incoming_diagnostic,)
    incoming_overflow = _overflow()
    stored_evidence = [diagnostic]
    stored_overflow = _overflow()
    expect_event = variant != "identical"
    if variant == "absent_present":
        stored_evidence = []
    elif variant == "reworded":
        incoming_diagnostic = _capture_payload("reworded diagnostic")
        incoming_evidence = (incoming_diagnostic,)
    elif variant == "reordered":
        second = _capture_payload("second diagnostic")
        stored_evidence = [diagnostic, second]
        incoming_evidence = (second, diagnostic)
    elif variant == "overflow":
        incoming_overflow = _overflow(truncated=True, distinct=1, occurrences=1)
    elif variant == "malformed":
        stored_evidence = "present-but-malformed"
    incoming = replace(
        baseline,
        diagnostic_evidence=incoming_evidence,
        diagnostic_overflow=incoming_overflow,
    )
    semantic_fingerprint = incoming.fingerprint()
    capsys.readouterr()
    results, stored_rows = await _record_semantic_replay(
        fixture,
        target_incoming=incoming,
        target_stored_evidence=stored_evidence,
        target_stored_overflow=stored_overflow,
    )
    captured = capsys.readouterr()
    assert len(results) == 48
    assert incoming.fingerprint() == semantic_fingerprint == baseline.fingerprint()
    assert stored_rows[1].raw_payload["h6a_evidence_fingerprint"] == (
        semantic_fingerprint
    )
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == (1 if expect_event else 0)
    if lines:
        event = ast.literal_eval(lines[0])
        assert event["event"] == "rob974_h6a_diagnostic_replay_divergence"
        assert set(event) == {
            "event",
            "idempotency_key_digest",
            "stored_diagnostic_digest",
            "incoming_diagnostic_digest",
        }
        assert "baseline diagnostic" not in lines[0]
        assert "reworded diagnostic" not in lines[0]


@pytest.mark.asyncio
async def test_concurrent_divergence_emits_one_digest_only_event_per_observation(
    tmp_path, capsys
):
    fixtures = [Fixture(tmp_path / f"case-{index}") for index in range(8)]
    incoming = [
        replace(
            fixture.attempts[0],
            diagnostic_evidence=(_capture_payload(f"concurrent-{index}"),),
            diagnostic_overflow=_overflow(),
        )
        for index, fixture in enumerate(fixtures)
    ]
    capsys.readouterr()
    await asyncio.gather(
        *(
            _record_semantic_replay(
                fixture,
                target_incoming=item,
                target_stored_evidence=[],
                target_stored_overflow=_overflow(),
            )
            for fixture, item in zip(fixtures, incoming, strict=True)
        )
    )
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 8
    events = [ast.literal_eval(line) for line in lines]
    assert all(
        event["event"] == "rob974_h6a_diagnostic_replay_divergence" for event in events
    )
    assert len({event["incoming_diagnostic_digest"] for event in events}) == 8
    assert not any("concurrent-" in line for line in lines)


@pytest.mark.asyncio
async def test_observer_sink_failure_cannot_change_primary_replay_result(
    tmp_path, monkeypatch
):
    fixture = Fixture(tmp_path)
    incoming = replace(
        fixture.attempts[0],
        diagnostic_evidence=(_capture_payload("sink failure diagnostic"),),
        diagnostic_overflow=_overflow(),
    )

    class BrokenSink:
        def write(self, _value):
            raise OSError("observer unavailable")

        def flush(self):
            raise OSError("observer unavailable")

    with monkeypatch.context() as context:
        context.setattr(bridge.sys, "stderr", BrokenSink())
        results, rows = await _record_semantic_replay(
            fixture,
            target_incoming=incoming,
            target_stored_evidence=[],
            target_stored_overflow=_overflow(),
        )
    assert len(results) == 48
    assert results[0] is rows[1]
    assert results[0].raw_payload["h6a_evidence_fingerprint"] == (
        incoming.fingerprint()
    )


def test_safe_failure_report_is_valid_bounded_json_without_exception_repr(tmp_path):
    # Static construction proves rendering never calls str/repr on the raw leaf.
    class ExplosiveError(RuntimeError):
        def __str__(self):
            raise AssertionError("raw exception was stringified")

        def __repr__(self):
            raise AssertionError("raw exception was repr'd")

    fixture = Fixture(tmp_path)
    capture = ActualRob970DiagnosticPort().capture_live_exception(
        _live_exception("safe"),
        catch_boundary="materializer",
        strategy="H6B",
        config_id="ROB-984",
    )
    outcome = materializer.MaterializationOutcome(
        exit_code=6,
        disposition="PRECOMMIT_FAILURE",
        trace=("preflight",),
        counters=materializer.CoordinatorCounters(
            session_factory=0,
            begin=0,
            register=0,
            h4=0,
            record=0,
            accounting=0,
            h5=0,
            stage=0,
            rollback=0,
            commit=0,
            publish=0,
            close=0,
        ),
        primary_error=ExplosiveError(),
        rollback_error=None,
        close_error=None,
        rollback_outcome="NOT_ATTEMPTED",
        close_outcome="NOT_ATTEMPTED",
        commit_confirmed=False,
        retry_forbidden=False,
        staged_pair=None,
        published_pair=None,
        accounting=None,
        scorecard=None,
        db_state="NOT_INSPECTED",
        artifact_state="NOT_INSPECTED",
        replay_inspection=None,
        diagnostic_capture=capture,
        diagnostic_capture_error=None,
    )
    del fixture
    rendered = materializer.render_safe_materialization_failure(outcome)
    parsed = json.loads(rendered)
    assert parsed["primary_error_type"] == "ExplosiveError"
    assert parsed["diagnostic"]["innermost_frame"]["function"] == "_live_exception"
    assert len(rendered) < 5000
