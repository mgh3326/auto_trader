"""ROB-970 (Q2/Q3, Fable-approved orch-fable-answer-rob970-20260719.md) --
RED/GREEN coverage for typed, sanitized, bounded child-failure diagnostic
evidence capture/dedupe, independent of H4/H5/H6 semantic identity.
"""

from __future__ import annotations

import pytest
from rob944_diagnostic_evidence import (
    MAX_DISTINCT_SIGNATURES,
    ChildFailureEvidence,
    DiagnosticOverflowMetadata,
    accumulate_diagnostic_evidence,
    capture_child_failure_evidence,
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
    # R2 audit: an explicit, visible truncation marker must actually be
    # present -- not merely a boolean flag -- and total length must respect
    # the bound.
    assert evidence.message.endswith("...<truncated>...")
    assert len(evidence.message) <= 500


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


def _compile_and_call(source: str, func_name: str, *, filename: str | None = None):
    """Compile+exec a SOURCE STRING as a synthetic module, with ``linecache``
    populated so the raised exception's traceback formatter actually
    retrieves and includes the REAL source line text (Python's traceback
    module only shows source context it can look up via linecache -- a bare
    ``compile(..., "<string>", ...)`` with no linecache entry silently omits
    it, which would make this repro a no-op). This reproduces a genuine
    oversized/unsafe SOURCE line inside the traceback's own frame context,
    matching the captain's exact repro shape.

    ``filename`` (R2 stop-gate audit): defaults to an auto-generated safe
    name, but a caller may pass a deliberately HOSTILE filename to prove
    the traceback's own ``File "..."`` frame line never leaks it."""
    import linecache

    # Deliberately NOT cleaned up in a `finally` here -- the raised exception
    # must still propagate to the CALLER with linecache intact so its
    # traceback formatter can retrieve the fake source line; popping the
    # cache entry before the caller captures the traceback would silently
    # make this repro a no-op (the exact bug this test file hit once).
    filename = filename or f"<diagnostic-evidence-test-{id(source)}>"
    lines = source.splitlines(keepends=True)
    linecache.cache[filename] = (len(source), None, lines, filename)
    namespace: dict = {}
    code = compile(source, filename, "exec")
    exec(code, namespace)  # noqa: S102 -- deliberate, test-only, no untrusted input
    namespace[func_name]()


def test_frame_aware_truncation_preserves_innermost_frame_header_despite_huge_source_line():
    """R2 audit repro: a failing source line carrying a ~6000-char comment
    must never push the innermost ``File ... line N, in boom`` frame header
    out of the bounded traceback via blind character-count tail-slicing."""
    huge_comment = "x" * 6000
    source = f'def boom():\n    raise ValueError("safe")  # {huge_comment}\n'
    try:
        _compile_and_call(source, "boom")
    except ValueError as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy="S1",
            config_id="S1-00",
        )
    assert evidence.truncated is True
    # R2 stop-gate audit: the TOTAL bound is exactly 4000 chars, INCLUDING
    # the explicit truncation marker itself -- not 4000 plus the marker's
    # own length.
    assert len(evidence.traceback_text) <= 4000
    assert "...<truncated>..." in evidence.traceback_text
    assert "ValueError" in evidence.traceback_text


def test_hostile_frame_filename_is_redacted_while_line_and_function_survive():
    """R2 stop-gate audit repro: a frame HEADER line's ``File "..."`` clause
    was previously exempted WHOLESALE from the residual-unsafe check, so a
    hostile FILENAME (shaped like a shouty secret label) survived untouched
    merely for living inside that clause. The filename must now be
    redacted while ``line N, in boom`` (frame identity) remains useful."""
    hostile_filename = "<SECRET-CREDENTIAL-BLOB-RAW-999>"
    source = 'def boom():\n    raise ValueError("safe")\n'
    try:
        _compile_and_call(source, "boom", filename=hostile_filename)
    except ValueError as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy="S1",
            config_id="S1-00",
        )
    assert hostile_filename not in evidence.traceback_text
    assert "SECRET" not in evidence.traceback_text
    assert "CREDENTIAL" not in evidence.traceback_text
    # frame identity remains useful despite the redacted filename.
    assert "in boom" in evidence.traceback_text
    assert "line 2" in evidence.traceback_text
    assert "ValueError" in evidence.traceback_text


def test_boundary_position_secret_straddling_the_line_truncation_cut_never_leaks_a_fragment():
    """R2 stop-gate audit repro: a shouty-secret-shaped word positioned so
    it straddles the per-line 300-char truncation cut must never leave a
    partial (but still recognizable) fragment in the output.

    Exercises ``_finalize_traceback`` directly (not the full
    ``capture_child_failure_evidence`` -> real-exception -> linecache
    pipeline) so the exact byte offset at which the secret word straddles
    the cut is fully controlled and deterministic -- calibrated against
    the CURRENT (pre-fix) behavior to confirm this is a genuine repro:
    running the residual-unsafe check AFTER length-bounding let the word
    get sliced mid-word (e.g. down to ``SECRE``), which no longer matches
    the residual pattern and survived undetected; running it BEFORE
    length-bounding (this module's current order) catches the full word
    first and wholesale-replaces the line."""
    from rob944_diagnostic_evidence import (
        _MAX_LINE_CHARS,
        _TRUNCATION_MARKER,
        _finalize_traceback,
    )

    keep_budget = _MAX_LINE_CHARS - len(_TRUNCATION_MARKER)
    preamble = '    raise ValueError("safe")  # '
    secret_comment = "SECRET-CREDENTIAL-BLOB-RAW-999-leaked-value-here"
    # Calibrated so the secret word starts 10 chars before the per-line cut
    # point -- straddling it (confirmed via a temporary pre-fix probe to
    # genuinely reproduce the leak, not merely avoid it by accident).
    prefix_len = max(keep_budget - len(preamble) - 10, 0)
    line = preamble + ("x" * prefix_len) + secret_comment
    raw_traceback = (
        "Traceback (most recent call last):\n"
        '  File "boom.py", line 2, in boom\n'
        f"{line}\n"
        "ValueError: safe\n"
    )
    out, _line_truncated = _finalize_traceback(raw_traceback, "safe", "safe")
    assert "SECRE" not in out
    assert "CREDENTIAL" not in out
    assert "leaked-value" not in out
    assert "ValueError: safe" in out
    assert 'File "boom.py", line 2, in boom' in out


def test_neutralize_non_frame_lines_redacts_unsafe_content_in_source_context_lines():
    """R2 audit repro: an indented SOURCE line is not automatically safe --
    ``capture_locals=False`` only guarantees no runtime VALUES appear, but
    the literal source text (e.g. a careless comment) can itself carry
    secret-shaped content and must still be redacted."""
    source = (
        "def boom():\n"
        '    raise ValueError("safe-message")  # SECRET-CREDENTIAL-BLOB-RAW-999\n'
    )
    try:
        _compile_and_call(source, "boom")
    except ValueError as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy="S1",
            config_id="S1-00",
        )
    assert "SECRET-CREDENTIAL-BLOB-RAW-999" not in evidence.traceback_text
    assert "SECRET-CREDENTIAL-BLOB-RAW-999" not in evidence.message
    # frame identity still survives.
    assert "ValueError" in evidence.traceback_text
    assert "line 2, in boom" in evidence.traceback_text


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
    merged, overflow = accumulate_diagnostic_evidence([e1, e2])
    assert len(merged) == 1
    assert merged[0].occurrence_count == 2
    # first-seen deterministic context is preserved, never overwritten by a
    # later duplicate.
    assert merged[0].symbol == "BTCUSDT"
    assert merged[0].fold_id == "fold-00"
    assert overflow == DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


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
    merged, overflow = accumulate_diagnostic_evidence([e1, e2])
    assert len(merged) == 2
    assert {m.occurrence_count for m in merged} == {1}
    assert overflow.truncated is False


def test_capture_redacts_quoted_env_dump_style_secret_keys():
    """R1 Critical-1 exact adversarial repro: a quoted dict-repr env dump
    (``{'OPENAI_API_KEY': '...'}``) is NOT caught by the old narrow
    unquoted-``key=value`` regex."""
    exc = _boom(
        "env={'OPENAI_API_KEY': 'sk-live-RAWSECRET123', 'TOKEN': 'opaque-secret'} "
        "raw_row=Bar1m(ts=123,open=99.1,high=100.2,low=98.7,close=100.0,volume=42.0)"
    )
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S1",
        config_id="S1-00",
    )
    for leaked in ("sk-live-RAWSECRET123", "opaque-secret", "99.1", "100.2", "42.0"):
        assert leaked not in evidence.message
        assert leaked not in evidence.traceback_text


@pytest.mark.parametrize(
    "message",
    [
        # mixed-case key names
        "Api_Key=sk-mixedCaseSecret1",
        "Password: hunter2mixedcase",
        "DSN: postgresql://user:hunter2mixed@host:5432/db",
        # quoted, single-quoted, and dict-style forms
        "config={'password': 'p4ssw0rd-quoted'}",
        '{"secret_token": "double-quoted-secret-value"}',
        # env-dump-shaped multi-key dict
        "{'AWS_SECRET_ACCESS_KEY': 'AKIA-FAKE-SECRET', 'DB_PASSWORD': 'p4ss'}",
        # JWT-shaped credential
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payloadpart",
        # absolute path
        "/Users/mgh3326/work/auto_trader.rob-970/.env contains SECRET=abc123",
    ],
)
def test_capture_redacts_every_secret_shaped_form(message):
    exc = _boom(message)
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="engine",
        strategy="S1",
        config_id="S1-00",
    )
    for leaked in (
        "sk-mixedCaseSecret1",
        "hunter2mixedcase",
        "hunter2mixed",
        "p4ssw0rd-quoted",
        "double-quoted-secret-value",
        "AKIA-FAKE-SECRET",
        "p4ss",
        "payloadpart",
        "/Users/mgh3326",
        "abc123",
    ):
        assert leaked not in evidence.message
        assert leaked not in evidence.traceback_text


def test_capture_redacts_raw_market_corpus_row_reprs():
    exc = _boom(
        "unexpected row TradeRecord(entry_ts=123,symbol='BTCUSDT',side='long',"
        "entry_price=100.5,exit_price=101.2,net_bps=12.3)"
    )
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="engine",
        strategy="S2",
        config_id="S2-00",
    )
    for leaked in ("100.5", "101.2", "12.3", "BTCUSDT"):
        assert leaked not in evidence.message


def test_capture_fails_closed_with_sentinel_on_unrecognized_residual_secret_shape():
    """Q3 (Fable-approved): structural redaction removes KNOWN patterns; a
    genuinely novel/unrecognized shape that STILL looks unsafe after
    structural redaction must fail closed -- the WHOLE message is replaced
    with the fixed sentinel, never left partially exposed."""
    exc = _boom(
        "novel-encoding-format !!SECRET-CREDENTIAL-BLOB!! "
        "client_secret::: 'zzz-never-matched-by-any-known-pattern-zzz' unresolved"
    )
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S1",
        config_id="S1-00",
    )
    assert evidence.message == "<redacted-unsafe-exception-message>"
    assert "zzz-never-matched-by-any-known-pattern-zzz" not in evidence.message
    # type/failing-frame identity survive even when the message is sentineled.
    assert evidence.exception_type == "ValueError"
    assert evidence.traceback_text
    assert "ValueError" in evidence.traceback_text


def test_capture_does_not_over_redact_an_already_safe_message():
    """The residual fail-closed check must not misfire on a message that
    was ALREADY safely and fully redacted -- only genuinely unsafe leftover
    content triggers the full-message sentinel."""
    exc = _boom("plain safe message about S2 confirmation_failed at fold-00")
    evidence = capture_child_failure_evidence(
        exc,
        transport="in_process",
        stage="generator",
        strategy="S1",
        config_id="S1-00",
    )
    assert evidence.message != "<redacted-unsafe-exception-message>"
    assert "confirmation_failed" in evidence.message


def test_capture_never_leaks_locals_env_or_raw_row_via_capture_locals_false():
    """capture_locals=False is permanent -- a local variable holding a raw
    secret/corpus row must never appear via frame-local dumps."""

    def _raise_with_local_secret():
        local_secret_var = "LOCAL-VAR-SECRET-NEVER-SHOWN"  # noqa: F841
        raise ValueError("generic failure message")

    try:
        _raise_with_local_secret()
    except ValueError as exc:
        evidence = capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage="engine",
            strategy="S1",
            config_id="S1-00",
        )
    assert "LOCAL-VAR-SECRET-NEVER-SHOWN" not in evidence.traceback_text
    assert "LOCAL-VAR-SECRET-NEVER-SHOWN" not in evidence.message


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


# ---------------------------------------------------------------------------
# ROB-970 R1 (Q1=A, cap=32, Fable-approved orch-fable-answer-rob970-r1-
# 20260719.md): bounded distinct-signature collection with honest overflow
# metadata -- first-32-distinct in canonical (first-seen) order, never an
# unbounded carrier.
# ---------------------------------------------------------------------------


def _distinct_evidence(n: int) -> list[ChildFailureEvidence]:
    return [
        capture_child_failure_evidence(
            _boom(f"distinct cause #{i}"),
            transport="in_process",
            stage="generator",
            strategy="S1",
            config_id="S1-00",
        )
        for i in range(n)
    ]


def test_max_distinct_signatures_constant_is_32():
    assert MAX_DISTINCT_SIGNATURES == 32


def test_cap_boundary_31_distinct_all_retained_no_overflow():
    events = _distinct_evidence(31)
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 31
    assert overflow == DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


def test_cap_boundary_32_distinct_all_retained_no_overflow():
    events = _distinct_evidence(32)
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 32
    assert overflow == DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


def test_cap_boundary_33_distinct_first_32_retained_33rd_overflows():
    events = _distinct_evidence(33)
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 32
    # canonical (first-seen execution) order -- the FIRST 32, not an
    # arbitrary/sorted subset.
    assert [e.signature for e in merged] == [e.signature for e in events[:32]]
    assert overflow == DiagnosticOverflowMetadata(
        truncated=True, omitted_distinct_signatures=1, omitted_occurrences=1
    )


def test_cap_repeated_signature_before_cap_is_full_bumps_occurrence_not_overflow():
    events = _distinct_evidence(31)
    events.append(events[0])  # repeat of an already-retained signature
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 31
    assert merged[0].occurrence_count == 2
    assert overflow == DiagnosticOverflowMetadata(
        truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
    )


def test_cap_repeated_signature_after_cap_full_bumps_omitted_occurrences_not_distinct():
    events = _distinct_evidence(33)  # 32 retained, #33 overflows
    events.append(events[32])  # a SECOND occurrence of the already-omitted #33
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 32
    assert overflow == DiagnosticOverflowMetadata(
        truncated=True, omitted_distinct_signatures=1, omitted_occurrences=2
    )


def test_cap_two_new_distinct_signatures_beyond_cap_each_count_once():
    events = _distinct_evidence(34)  # 32 retained, #33 and #34 overflow
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == 32
    assert overflow == DiagnosticOverflowMetadata(
        truncated=True, omitted_distinct_signatures=2, omitted_occurrences=2
    )


def test_cap_boundary_replay_merge_is_deterministic_and_observer_effect_0():
    """Feeding the same 40 distinct events in two independently-built but
    identically-ordered runs must produce byte-identical evidence/overflow --
    no wall-clock/UUID/hash-seed-dependent behavior."""
    events_a = _distinct_evidence(40)
    events_b = _distinct_evidence(40)  # independently captured, same messages
    merged_a, overflow_a = accumulate_diagnostic_evidence(events_a)
    merged_b, overflow_b = accumulate_diagnostic_evidence(events_b)
    assert [e.signature for e in merged_a] == [e.signature for e in merged_b]
    assert (
        overflow_a
        == overflow_b
        == DiagnosticOverflowMetadata(
            truncated=True, omitted_distinct_signatures=8, omitted_occurrences=8
        )
    )


def test_serialized_capped_payload_size_is_bounded():
    """Even with far more distinct failures than the cap, the serialized
    size of the retained evidence stays bounded (never proportional to the
    number of distinct failures encountered)."""
    import json

    events = _distinct_evidence(500)
    merged, overflow = accumulate_diagnostic_evidence(events)
    assert len(merged) == MAX_DISTINCT_SIGNATURES
    serialized = json.dumps(
        [
            {
                "transport": e.transport,
                "stage": e.stage,
                "exception_type": e.exception_type,
                "message": e.message,
                "traceback_text": e.traceback_text,
                "occurrence_count": e.occurrence_count,
            }
            for e in merged
        ]
    )
    # bounded by cap * per-entry max content, NOT by the 500 distinct inputs.
    assert len(serialized) < MAX_DISTINCT_SIGNATURES * 5_000
    assert overflow.omitted_distinct_signatures == 500 - MAX_DISTINCT_SIGNATURES
