"""ROB-1060 H2-lock adversarial-verification NEW-1 and NEW-2 (2026-07-26).

Two non-blocking findings from an independent adversarial verification pass
that returned PASS / merge recommended on the H2 seal, closed here rather
than deferred to H3:

NEW-1: ``build_registration_plan()``'s ``SemanticHashDriftError`` (Finding 4)
proves the pinned digest binds the ARTIFACT (``sealed.semantic_hash()``). It
does NOT prove the digest binds the PLAN this function derives from that
artifact and returns. The verifier demonstrated that mutating every spec's
``components`` (e.g. forcing ``cost.primary`` to a relaxed cost scenario)
AFTER the digest check passes Finding 4's gate, keeps
``validate_same_family_components_are_identical`` satisfied (a uniform
mutation stays internally consistent within each family), and a fully green
test suite -- while ``_cmd_register`` would feed the relaxed, mutated
components into ``StrategyExperimentIdentity`` under the CORRECT pinned
digest. ``build_registration_plan()`` now asserts every spec's ``components``
equals a fresh, independent recomputation
(``sealed.to_dict()["identity_components"][config_id]``) immediately before
returning, raising ``PlanComponentDivergenceError`` on any divergence.

NEW-2: Finding 5 correctly DETERMINED that wiring
``identity.assert_supersession_preserves_sealed_components`` into
``_cmd_register`` is genuinely blocked on an ``app.*``/DB read this pure H2
package must not add, and left it documented-but-unwired via a code comment.
A comment is not fail-closed: a future edit that adds
``supersedes_experiment_id=...`` to ``_cmd_register`` without ALSO wiring
that guard would silently register a superseding experiment whose components
were never checked against its parent's.
``_assert_supersession_guard_wired_before_use`` converts that from a silent
pass into ``SupersessionGuardUnwiredError``.
"""

from __future__ import annotations

import pytest


def test_build_registration_plan_succeeds_when_undrifted_new1_baseline():
    """Happy-path control for NEW-1 -- the added assertion must not disturb
    the existing undrifted behavior Finding 4's own baseline test covers."""
    import registry_cli as m

    plan = m.build_registration_plan()
    assert len(plan["semantic_hash"]) == 64
    assert plan["config_count"] == 16
    assert len(plan["specs"]) == 16


def test_build_registration_plan_fails_closed_when_a_fresh_recomputation_diverges(
    monkeypatch,
):
    """Reproduces the shape of the verifier's exact adversarial finding: a
    correct, matching pinned digest (Finding 4's gate is untouched here)
    attached to per-config identity components that no longer match a fresh,
    independent recomputation of the same sealed artifact.

    We inject the divergence at the second (and only the second)
    ``SealedArtifact.to_dict()`` call rather than by mutating ``specs``
    in-process (which isn't an addressable seam from outside the function
    body) -- ``build_registration_plan()`` calls ``to_dict()`` exactly twice:
    once (indirectly, via ``semantic_hash()``) for the Finding-4 digest
    check, and once (directly) for this NEW-1 check. Mutating only the
    second call's output reproduces precisely the property the verifier
    exploited: a digest that still matches the pin, next to components that
    no longer match what the artifact actually contains. The scratch-copy
    verification in the ROB-1060 report reproduces the verifier's literal
    ``for _s in specs: ...`` mutation for a byte-for-byte proof; this test is
    the permanent CI regression guard.
    """
    import artifact as art
    import registry_cli as m

    original_to_dict = art.SealedArtifact.to_dict
    calls = {"n": 0}

    def _to_dict_diverging_on_second_call(self):
        d = original_to_dict(self)
        calls["n"] += 1
        if calls["n"] > 1:
            for components in d["identity_components"].values():
                components["cost"] = {**components["cost"], "primary": "C50"}
        return d

    monkeypatch.setattr(
        art.SealedArtifact, "to_dict", _to_dict_diverging_on_second_call
    )

    with pytest.raises(m.PlanComponentDivergenceError, match=r"'cost'"):
        m.build_registration_plan()


def test_supersession_guard_wired_before_use_is_a_noop_for_none():
    """No-op control: today's actual `_cmd_register` behavior (every
    identity is a fresh registration, `supersedes_experiment_id=None`) must
    remain unaffected by the new guard."""
    import registry_cli as m

    m._assert_supersession_guard_wired_before_use(None)  # must not raise


def test_supersession_guard_wired_before_use_fails_closed_on_any_non_none_value():
    """Reproduces NEW-2's exact scenario: a future edit sets
    `supersedes_experiment_id` to a real (non-`None`) experiment id without
    wiring `identity.assert_supersession_preserves_sealed_components`. Must
    raise `SupersessionGuardUnwiredError`, not silently proceed."""
    import registry_cli as m

    with pytest.raises(
        m.SupersessionGuardUnwiredError, match="has not been wired into _cmd_register"
    ):
        m._assert_supersession_guard_wired_before_use("some-parent-experiment-id")
