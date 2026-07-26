"""ROB-1060 H2-lock adversarial-verification Finding 4 (2026-07-26):
``build_registration_plan()`` must assert the pinned semantic digest and
fail closed on mismatch, not merely emit it as an unchecked output field.

Before this test/guard existed, the seal was a TEST-TIME lock only:
``build_registration_plan()`` computed ``semantic_hash`` and put it in its
return value, but never compared it to
``artifact.SEALED_ARTIFACT_SEMANTIC_HASH`` -- a drifted artifact would be
"registered" (or planned) without complaint at runtime; only the (CI-absent,
see Finding 1) test suite would ever notice.
"""

from __future__ import annotations

import pytest


def test_build_registration_plan_succeeds_when_undrifted():
    import registry_cli as m

    plan = m.build_registration_plan()
    assert len(plan["semantic_hash"]) == 64
    assert plan["config_count"] == 16


def test_build_registration_plan_fails_closed_on_a_drifted_pinned_digest(monkeypatch):
    """Simulates the exact runtime-drift scenario Finding 4 describes: the
    artifact module's pinned constant no longer matches what the (unchanged)
    sealed configs/params actually produce -- e.g. because a sibling module
    drifted after the pin was last updated. ``build_registration_plan()``
    must raise, not silently return a plan built from the mismatched
    artifact."""
    import artifact as art
    import registry_cli as m

    monkeypatch.setattr(art, "SEALED_ARTIFACT_SEMANTIC_HASH", "0" * 64)
    with pytest.raises(m.SemanticHashDriftError, match="does not match the pinned"):
        m.build_registration_plan()


def test_plan_subcommand_propagates_the_drift_failure_as_a_nonzero_exit(monkeypatch):
    """End-to-end proof at the CLI boundary: ``_cmd_plan`` must not swallow
    the drift error and print a plan anyway."""
    import artifact as art
    import registry_cli as m

    monkeypatch.setattr(art, "SEALED_ARTIFACT_SEMANTIC_HASH", "0" * 64)
    with pytest.raises(m.SemanticHashDriftError):
        m.main(["plan"])
