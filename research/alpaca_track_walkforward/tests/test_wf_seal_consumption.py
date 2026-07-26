"""ROB-1062 H4 — seal_consumption reads H2's seal, never re-declares it.

The critical property under test: ``fold_schedule.py``'s module-level
constants (``OOS_FOLDS``/``OOS_DAYS``/``TRAIN_DAYS``/``EMBARGO_DAYS``/
``ROLL_DAYS``) are a SEPARATE, hardcoded copy for a documented reason (the
schedule generator must have zero runtime H2 dependency — it is pure
calendar arithmetic used inside the fill model's hot path). Because they are
a copy, not a read, ``assert_policy_matches_schedule_constants`` exists
SPECIFICALLY to catch the two copies drifting apart — this test proves that
cross-check actually fires.
"""

from __future__ import annotations

import fold_schedule as fs
import pytest
import wf_seal_consumption as sc


def test_sealed_walk_forward_shape_matches_run_a_literal_values():
    assert sc.oos_folds() == 8
    assert sc.oos_days() == 28
    assert sc.train_days() == 365
    assert sc.embargo_days() == 7
    assert sc.roll_days() == 28
    assert sc.min_modeled_entries_per_fold() == 5


def test_cost_scenarios_bp_are_exactly_the_sealed_four():
    assert sc.cost_scenarios_bp() == {"C50": 50, "C100": 100, "C120": 120, "C150": 150}
    assert sc.primary_cost_scenario() == "C120"
    assert sc.upward_cost_scenario() == "C150"


def test_ap_a2_turnover_band_matches_seal():
    assert sc.ap_a2_turnover_band() == (0.2083, 0.2885)


def test_stress_annual_cost_cap_pct_per_family():
    assert sc.stress_annual_cost_cap_pct("AP-A1") == 6
    assert sc.stress_annual_cost_cap_pct("AP-A2") == 18


def test_stress_annual_cost_cap_pct_rejects_unknown_family():
    with pytest.raises(ValueError, match="unknown family"):
        sc.stress_annual_cost_cap_pct("AP-A3")


def test_assert_policy_matches_schedule_constants_passes_for_the_real_constants():
    sc.assert_policy_matches_schedule_constants(
        oos_folds_const=fs.OOS_FOLDS,
        oos_days_const=fs.OOS_DAYS,
        train_days_const=fs.TRAIN_DAYS,
        embargo_days_const=fs.EMBARGO_DAYS,
        roll_days_const=fs.ROLL_DAYS,
    )


def test_assert_policy_matches_schedule_constants_fails_closed_on_drift():
    """Proves the cross-check is a REAL guard, not vacuous: a fold_schedule
    copy that silently drifted (e.g. embargo shortened 7->0) is caught."""
    with pytest.raises(sc.SealDriftError, match="embargo_days"):
        sc.assert_policy_matches_schedule_constants(
            oos_folds_const=fs.OOS_FOLDS,
            oos_days_const=fs.OOS_DAYS,
            train_days_const=fs.TRAIN_DAYS,
            embargo_days_const=0,
            roll_days_const=fs.ROLL_DAYS,
        )
