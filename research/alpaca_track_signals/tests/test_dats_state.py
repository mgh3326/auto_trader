"""ROB-1061 H3 (Run A SS11.3, AC6/AC7/AC8/AC9) — the AP-A1 DATS per-asset
state-transition boundary contract. This is the FIRST RED test written for
H3: ``dats_state`` does not exist yet.

Boundary contract, verbatim from the authority doc (SHA-256 67b5d3c2...):

    entry: flat AND D >= +0.005 AND R > 0
    exit:  long AND (D <= -0.005 OR R <= 0)
    hysteresis: -0.005 < D < +0.005 keeps existing state (long stays long)

Entry is per-asset 0->1 ONLY (AC9) — a currently-long symbol can never
"enter" again (no re-entry/pyramiding), enforced structurally (the
transition function takes the CURRENT state and only evaluates the branch
that state allows), not by convention.
"""

from __future__ import annotations

import dats_state as ds
import pytest

THRESHOLD = 0.005


def test_flat_enters_at_the_exact_positive_threshold_with_positive_r():
    # D == +0.005 exactly (boundary INCLUSIVE per AC8) AND R > 0 -> ENTER.
    outcome = ds.classify_transition(state="flat", d=0.005, r=0.01, threshold=THRESHOLD)
    assert outcome.action == "ENTER"


def test_flat_does_not_enter_one_ulp_below_the_positive_threshold():
    just_below = 0.005 - 1e-12
    outcome = ds.classify_transition(
        state="flat", d=just_below, r=0.01, threshold=THRESHOLD
    )
    assert outcome.action != "ENTER"


def test_flat_requires_r_strictly_positive_not_just_nonnegative():
    # D condition satisfied, but R == 0 exactly must NOT allow entry
    # (entry requires R > 0, strictly -- R == 0 is not "R > 0").
    outcome = ds.classify_transition(state="flat", d=0.01, r=0.0, threshold=THRESHOLD)
    assert outcome.action != "ENTER"


def test_long_exits_at_the_exact_negative_threshold():
    # D == -0.005 exactly (boundary INCLUSIVE, exit fires) per AC8.
    outcome = ds.classify_transition(
        state="long", d=-0.005, r=0.01, threshold=THRESHOLD
    )
    assert outcome.action == "EXIT"


def test_long_holds_one_ulp_inside_the_negative_threshold_hysteresis_band():
    just_inside = -0.005 + 1e-12
    outcome = ds.classify_transition(
        state="long", d=just_inside, r=0.01, threshold=THRESHOLD
    )
    assert outcome.action == "HOLD"


def test_long_exits_when_r_is_exactly_zero_regardless_of_d():
    # R <= 0 triggers exit even when D is deep in "healthy long" territory.
    outcome = ds.classify_transition(state="long", d=0.02, r=0.0, threshold=THRESHOLD)
    assert outcome.action == "EXIT"


def test_long_holds_through_the_entire_hysteresis_band_when_r_stays_positive():
    for d in (-0.0049, -0.001, 0.0, 0.001, 0.0049):
        outcome = ds.classify_transition(state="long", d=d, r=0.01, threshold=THRESHOLD)
        assert outcome.action == "HOLD", f"d={d} should hold, got {outcome.action}"


def test_a_long_symbol_can_never_receive_an_enter_action_no_reentry_pyramiding():
    # AC9: per-asset 0->1 transition ONLY. Sweep the entire domain that would
    # trigger entry if evaluated as if flat -- a long position must never
    # re-enter.
    for d in (0.005, 0.01, 1.0):
        for r in (0.001, 0.5, 5.0):
            outcome = ds.classify_transition(
                state="long", d=d, r=r, threshold=THRESHOLD
            )
            assert outcome.action != "ENTER"


def test_unknown_state_is_rejected_fail_closed():
    with pytest.raises(ValueError, match="state"):
        ds.classify_transition(state="short", d=0.01, r=0.01, threshold=THRESHOLD)
