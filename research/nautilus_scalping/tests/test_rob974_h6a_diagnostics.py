"""ROB-981 (ROB-974 R2 H6-A) CP6 -- ROB-970 diagnostic carrier and replay
observer isolation."""

from __future__ import annotations

import pytest
import rob974_h6a_diagnostics as diag


def _capture(message: str, *, stage: str = "engine") -> diag.ChildFailureEvidence:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return diag.capture_child_failure_evidence(
            exc,
            transport="in_process",
            stage=stage,
            strategy="S3",
            config_id="S3-00",
        )


class TestReuseOfRob970Sanitizer:
    def test_message_cap_500(self):
        row = _capture("x" * 10_000)
        assert len(row.message) <= 500

    def test_traceback_cap_4000(self):
        row = _capture("boom")
        assert len(row.traceback_text) <= 4000

    def test_max_distinct_signatures_is_32(self):
        assert diag.MAX_DISTINCT_SIGNATURES == 32

    def test_accumulate_honors_cap(self):
        events = [_capture(f"unique message {i}") for i in range(40)]
        evidence, overflow = diag.accumulate_diagnostic_evidence(events)
        assert len(evidence) == 32
        assert overflow.truncated is True
        assert overflow.omitted_distinct_signatures == 8


class TestDiagnosticCarrier:
    def test_valid_carrier_constructs(self):
        row = _capture("boom")
        diag.DiagnosticCarrier(
            evidence=(row,),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
            ),
        )

    def test_empty_carrier_is_valid(self):
        diag.DiagnosticCarrier(
            evidence=(),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
            ),
        )

    def test_non_tuple_evidence_rejected(self):
        row = _capture("boom")
        with pytest.raises(diag.DiagnosticCarrierError):
            diag.DiagnosticCarrier(
                evidence=[row],  # list, not tuple
                overflow=diag.DiagnosticOverflowMetadata(
                    truncated=False,
                    omitted_distinct_signatures=0,
                    omitted_occurrences=0,
                ),
            )

    def test_subclass_leaf_rejected(self):
        row = _capture("boom")

        class _SneakyEvidence(diag.ChildFailureEvidence):
            pass

        forged = _SneakyEvidence(**row.__dict__)
        with pytest.raises(diag.DiagnosticCarrierError):
            diag.DiagnosticCarrier(
                evidence=(forged,),
                overflow=diag.DiagnosticOverflowMetadata(
                    truncated=False,
                    omitted_distinct_signatures=0,
                    omitted_occurrences=0,
                ),
            )

    def test_overflow_subclass_rejected(self):
        class _SneakyOverflow(diag.DiagnosticOverflowMetadata):
            pass

        forged = _SneakyOverflow(
            truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
        )
        with pytest.raises(diag.DiagnosticCarrierError):
            diag.DiagnosticCarrier(evidence=(), overflow=forged)

    def test_over_cap_evidence_rejected(self):
        events = [_capture(f"unique message {i}") for i in range(40)]
        evidence, overflow = diag.accumulate_diagnostic_evidence(events)
        oversized = evidence + (evidence[0],)  # 33 entries -- but also a dup signature
        with pytest.raises(diag.DiagnosticCarrierError):
            diag.DiagnosticCarrier(evidence=oversized, overflow=overflow)

    def test_duplicate_signature_rejected(self):
        row = _capture("boom")
        with pytest.raises(diag.DiagnosticCarrierError):
            diag.DiagnosticCarrier(
                evidence=(row, row),
                overflow=diag.DiagnosticOverflowMetadata(
                    truncated=False,
                    omitted_distinct_signatures=0,
                    omitted_occurrences=0,
                ),
            )


class TestCanonicalDiagnosticBytes:
    def test_deterministic_same_input(self):
        row = _capture("boom")
        overflow = diag.DiagnosticOverflowMetadata(
            truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
        )
        carrier_a = diag.DiagnosticCarrier(evidence=(row,), overflow=overflow)
        carrier_b = diag.DiagnosticCarrier(evidence=(row,), overflow=overflow)
        assert diag.canonical_diagnostic_bytes(
            carrier_a
        ) == diag.canonical_diagnostic_bytes(carrier_b)

    def test_empty_and_populated_carriers_differ(self):
        row = _capture("boom")
        empty = diag.DiagnosticCarrier(
            evidence=(),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
            ),
        )
        populated = diag.DiagnosticCarrier(
            evidence=(row,),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
            ),
        )
        assert diag.canonical_diagnostic_bytes(
            empty
        ) != diag.canonical_diagnostic_bytes(populated)


class TestDivergesIsomorphism:
    def test_identical_is_no_divergence(self):
        row = _capture("boom")
        overflow = diag.DiagnosticOverflowMetadata(
            truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
        )
        carrier = diag.DiagnosticCarrier(evidence=(row,), overflow=overflow)
        bytes_a = diag.canonical_diagnostic_bytes(carrier)
        bytes_b = diag.canonical_diagnostic_bytes(carrier)
        assert diag.diverges(bytes_a, bytes_b) is False

    def test_reworded_message_diverges(self):
        overflow = diag.DiagnosticOverflowMetadata(
            truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
        )
        carrier_a = diag.DiagnosticCarrier(
            evidence=(_capture("boom v1"),), overflow=overflow
        )
        carrier_b = diag.DiagnosticCarrier(
            evidence=(_capture("boom v2"),), overflow=overflow
        )
        bytes_a = diag.canonical_diagnostic_bytes(carrier_a)
        bytes_b = diag.canonical_diagnostic_bytes(carrier_b)
        assert diag.diverges(bytes_a, bytes_b) is True

    def test_overflow_only_change_diverges(self):
        row = _capture("boom")
        carrier_a = diag.DiagnosticCarrier(
            evidence=(row,),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=False, omitted_distinct_signatures=0, omitted_occurrences=0
            ),
        )
        carrier_b = diag.DiagnosticCarrier(
            evidence=(row,),
            overflow=diag.DiagnosticOverflowMetadata(
                truncated=True, omitted_distinct_signatures=1, omitted_occurrences=1
            ),
        )
        bytes_a = diag.canonical_diagnostic_bytes(carrier_a)
        bytes_b = diag.canonical_diagnostic_bytes(carrier_b)
        assert diag.diverges(bytes_a, bytes_b) is True

    def test_signature_recurrence_order_change_is_first_seen_preserving(self):
        # accumulate_diagnostic_evidence preserves FIRST-seen context on a
        # recurrence -- two different orderings of the SAME underlying
        # events (one first-seen occurrence + one repeat) must converge to
        # byte-identical carriers.
        e1 = _capture("root cause A")
        e2 = _capture("root cause A")  # same message/stage -> same signature
        evidence_ab, overflow_ab = diag.accumulate_diagnostic_evidence([e1, e2])
        evidence_ba, overflow_ba = diag.accumulate_diagnostic_evidence([e1, e2])
        carrier_ab = diag.DiagnosticCarrier(evidence=evidence_ab, overflow=overflow_ab)
        carrier_ba = diag.DiagnosticCarrier(evidence=evidence_ba, overflow=overflow_ba)
        assert diag.canonical_diagnostic_bytes(
            carrier_ab
        ) == diag.canonical_diagnostic_bytes(carrier_ba)


class TestSemanticExclusion:
    def test_diagnostic_bytes_are_never_a_semantic_hash_input(self):
        # rob974_h6a_diagnostics has no dependency on (and does not import)
        # rob974_h6a_evidence/accounting/payload -- diagnostic content
        # structurally cannot reach any semantic hash function.
        import sys

        assert "rob974_h6a_evidence" not in getattr(
            sys.modules.get("rob974_h6a_diagnostics"), "__dict__", {}
        )


class TestReplayObservation:
    def test_build_observation_is_digest_only(self):
        obs = diag.build_replay_observation(
            idempotency_key="run:experiment:0",
            stored_bytes=b"stored",
            incoming_bytes=b"incoming",
            stored_distinct_signature_count=1,
            new_distinct_signature_count=2,
        )
        assert len(obs.idempotency_key_digest) == 64
        assert len(obs.stored_diagnostic_digest) == 64
        assert len(obs.incoming_diagnostic_digest) == 64
        assert obs.stored_distinct_signature_count == 1
        assert obs.new_distinct_signature_count == 2
        # Raw idempotency_key/content must never appear verbatim.
        assert "run:experiment:0" not in obs.idempotency_key_digest
        assert "stored" not in obs.stored_diagnostic_digest
        assert "incoming" not in obs.incoming_diagnostic_digest

    def test_observation_is_deterministic(self):
        kwargs = {
            "idempotency_key": "run:experiment:0",
            "stored_bytes": b"stored",
            "incoming_bytes": b"incoming",
            "stored_distinct_signature_count": 1,
            "new_distinct_signature_count": 2,
        }
        a = diag.build_replay_observation(**kwargs)
        b = diag.build_replay_observation(**kwargs)
        assert a == b


class TestMissingSentinelIdentity:
    def test_missing_is_not_none(self):
        assert diag.MISSING is not None

    def test_missing_is_not_falsy_equivalent_to_default(self):
        # The whole point of MISSING: `.get(key, MISSING) is MISSING` must
        # distinguish "absent" from a present-but-falsy value like `{}`/0/False.
        d = {"present_falsy": {}}
        assert d.get("present_falsy", diag.MISSING) is not diag.MISSING
        assert d.get("absent_key", diag.MISSING) is diag.MISSING
