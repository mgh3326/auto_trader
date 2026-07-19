"""ROB-970 (Q2/Q3, Fable-approved orch-fable-answer-rob970-20260719.md) --
RED/GREEN coverage for typed, sanitized, bounded child-failure diagnostic
evidence capture/dedupe, independent of H4/H5/H6 semantic identity.
"""

from __future__ import annotations

import pytest
from rob944_diagnostic_evidence import (
    ChildFailureEvidence,
    capture_child_failure_evidence,
    merge_child_failure_evidence,
)


def _boom(message: str) -> Exception:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


def test_capture_basic_shape_in_process_transport():
    exc = _boom("duplicate/colliding rejection signal_ts for S2/S2-00/BTCUSDT/fold-00")
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S2",
        config_id="S2-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
    )
    assert isinstance(evidence, ChildFailureEvidence)
    assert evidence.transport == "in_process"
    assert evidence.stage == "generator"
    assert evidence.exception_type == "ValueError"
    assert "duplicate/colliding rejection signal_ts" in evidence.message
    assert evidence.traceback_text  # non-empty
    assert "ValueError" in evidence.traceback_text
    assert evidence.stderr is None
    assert evidence.strategy == "S2"
    assert evidence.config_id == "S2-00"
    assert evidence.symbol == "BTCUSDT"
    assert evidence.fold_id == "fold-00"
    assert evidence.occurrence_count == 1
    assert evidence.truncated is False
    assert len(evidence.signature) == 64  # hex64 sha256


def test_capture_is_deterministic_same_failure_same_signature():
    exc1 = _boom("boom message")
    exc2 = _boom("boom message")
    e1 = capture_child_failure_evidence(
        exc1,
        transport="in_process",
        stage="funding_gate",
        strategy="S1",
        config_id="S1-00",
        symbol="ETHUSDT",
        fold_id="fold-03",
    )
    e2 = capture_child_failure_evidence(
        exc2,
        transport="in_process",
        stage="funding_gate",
        strategy="S1",
        config_id="S1-00",
        symbol="XRPUSDT",  # different symbol/fold -- must not affect signature
        fold_id="fold-05",
    )
    assert e1.signature == e2.signature


def test_capture_never_leaks_absolute_worktree_path():
    exc = _boom("engine crashed")
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="engine",
        strategy="S2",
        config_id="S2-00",
    )
    assert "/Users/" not in evidence.traceback_text
    assert "/Users/" not in evidence.message
    assert "auto_trader" not in evidence.traceback_text
    # the bare filename must still survive -- this is a *sanitized*, not an
    # *emptied*, traceback.
    assert "test_rob944_diagnostic_evidence.py" in evidence.traceback_text


def test_capture_redacts_dsn_and_secret_like_substrings():
    exc = _boom(
        "connect failed dsn=postgresql://user:hunter2@localhost:5432/prod "
        "api_key=sk-ABCDEF0123456789 token: eyJhbGciOiJIUzI1NiJ9.secretpayload"
    )
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S1",
        config_id="S1-00",
    )
    for leaked in (
        "hunter2",
        "sk-ABCDEF0123456789",
        "eyJhbGciOiJIUzI1NiJ9.secretpayload",
        "postgresql://user:hunter2",
    ):
        assert leaked not in evidence.message
        assert leaked not in evidence.traceback_text


def test_capture_bounds_and_truncates_oversized_message_with_marker():
    huge = "x" * 10_000
    exc = _boom(huge)
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="engine",
        strategy="S2",
        config_id="S2-01",
    )
    assert evidence.truncated is True
    assert len(evidence.message) < 10_000
    assert evidence.message.startswith("x")


def test_capture_bounds_traceback_but_retains_type_message_and_failing_frame():
    # A long `raise ... from ...` chain -- each link contributes its OWN
    # non-collapsed frame block (unlike same-frame recursion, which Python's
    # traceback formatter collapses via "[Previous line repeated N times]"),
    # so this reliably exceeds the bound and forces real truncation.
    current: BaseException | None = None
    for i in range(120):
        try:
            if current is None:
                raise ValueError(f"link {i}")
            raise ValueError(f"link {i}") from current
        except ValueError as exc:
            current = exc
    try:
        raise RuntimeError("deep failure at the bottom") from current
    except RuntimeError as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy="S2",
            config_id="S2-00",
        )
    assert evidence.truncated is True
    assert evidence.exception_type == "RuntimeError"
    assert "deep failure at the bottom" in evidence.message
    # the failing (innermost) frame must survive truncation, not just the
    # outermost/oldest frames.
    assert "deep failure at the bottom" in evidence.traceback_text
    assert "RuntimeError" in evidence.traceback_text


def test_merge_aggregates_repeated_identical_signature_with_occurrence_count():
    exc = _boom("same root cause")
    e1 = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S2",
        config_id="S2-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
    )
    e2 = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S2",
        config_id="S2-00",
        symbol="ETHUSDT",
        fold_id="fold-01",
    )
    merged = merge_child_failure_evidence((), e1)
    merged = merge_child_failure_evidence(merged, e2)
    assert len(merged) == 1
    assert merged[0].occurrence_count == 2
    # first-seen deterministic context is preserved, never overwritten by a
    # later duplicate.
    assert merged[0].symbol == "BTCUSDT"
    assert merged[0].fold_id == "fold-00"


def test_merge_keeps_distinct_signatures_separate():
    e1 = capture_child_failure_evidence(
        _boom("cause A"),
        transport="in_process",
        stage="generator",
        strategy="S2",
        config_id="S2-00",
    )
    e2 = capture_child_failure_evidence(
        _boom("cause B"),
        transport="in_process",
        stage="funding_gate",
        strategy="S2",
        config_id="S2-00",
    )
    merged = merge_child_failure_evidence((), e1)
    merged = merge_child_failure_evidence(merged, e2)
    assert len(merged) == 2
    assert {m.occurrence_count for m in merged} == {1}


def test_only_in_process_transport_supported_today_and_stderr_is_never_fabricated():
    exc = _boom("x")
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="engine",
        strategy="S1",
        config_id="S1-00",
    )
    assert evidence.stderr is None
    with pytest.raises(ValueError):
        capture_child_failure_evidence(
            exc,
            transport="subprocess",  # not yet a real capture path -- reject, never fabricate
            stage="engine",
            strategy="S1",
            config_id="S1-00",
        )
