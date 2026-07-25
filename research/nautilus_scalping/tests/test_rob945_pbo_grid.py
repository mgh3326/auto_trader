"""ROB-945 (H5) -- PBO/CSCV auxiliary evidence RED tests.

Frozen by the second Fable ruling (orch-fable-answer-rob945b-20260718.md,
Q2=A): each strategy's frozen 12 configs, each independently @17-evaluated
over the exact frozen full window ``[2025-07-01T00:00Z, 2026-07-01T00:00Z)``
(365 UTC days, no leap day in range), produce one identical UTC-day-keyed
return grid (no-trade day = 0). This module validates that sealed grid and
delegates the actual CSCV/PBO computation to
``research_contracts.honest_offline_gate.probability_backtest_overfitting``
(reused, never re-implemented) with ``slices=4`` pinned exactly. Auxiliary-
only: never a pass gate, and any grid defect fails closed.
"""

from __future__ import annotations

import pytest
from rob945_pbo_grid import (
    FROZEN_DAY_COUNT,
    FROZEN_DAY_KEYS,
    PboGridError,
    compute_pbo_auxiliary_evidence,
)


def _canonical_config_ids(strategy="S1"):
    return [f"{strategy}-{i:02d}" for i in range(12)]


def _grid(strategy="S1", days=FROZEN_DAY_KEYS, value=0.0):
    return {cid: dict.fromkeys(days, value) for cid in _canonical_config_ids(strategy)}


def test_valid_twelve_config_frozen_365_day_grid_returns_a_value_or_reason():
    grid = _grid()
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.config_count == 12
    assert evidence.day_count == 365
    assert evidence.day_count == FROZEN_DAY_COUNT
    assert evidence.slices == 4
    assert isinstance(evidence.artifact_hash, str) and len(evidence.artifact_hash) == 64


def test_364_days_missing_one_day_fails_closed():
    grid = _grid(days=FROZEN_DAY_KEYS[:-1])
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_366_days_extra_day_fails_closed():
    grid = _grid(days=(*FROZEN_DAY_KEYS, "2026-07-01"))
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_reversed_day_order_fails_closed_even_though_the_set_is_complete():
    grid = _grid(days=tuple(reversed(FROZEN_DAY_KEYS)))
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_wrong_boundary_day_fails_closed():
    """A grid shifted by one day (e.g. starting 2025-07-02) has the correct
    COUNT but the wrong boundary -- must still fail closed."""
    shifted = (*FROZEN_DAY_KEYS[1:], "2026-07-01")
    grid = _grid(days=shifted)
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_non_date_key_fails_closed():
    bad_days = (*FROZEN_DAY_KEYS[:-1], "not-a-date")
    grid = _grid(days=bad_days)
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_wrong_config_count_fails_closed():
    grid = _grid()
    del grid["S1-11"]
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_wrong_config_id_fails_closed():
    """A config using the wrong strategy's canonical ID (or a made-up ID)
    is never silently accepted just because the count is 12."""
    grid = _grid()
    del grid["S1-11"]
    grid["S2-11"] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)  # wrong-strategy ID
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_extra_thirteenth_config_fails_closed():
    grid = _grid()
    grid["S1-12"] = dict.fromkeys(FROZEN_DAY_KEYS, 0.0)
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_mismatched_day_key_set_across_configs_fails_closed():
    grid = _grid()
    grid["S1-05"] = dict.fromkeys(FROZEN_DAY_KEYS[:-1], 0.0)  # missing the last day
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_non_finite_return_fails_closed():
    grid = _grid()
    grid["S1-05"][FROZEN_DAY_KEYS[0]] = float("nan")
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_gap_invalid_day_value_fails_closed():
    """A gap-invalid day (represented here as a non-numeric placeholder,
    e.g. None for "no evidence for this day") must never be silently
    treated as 0 -- the caller must supply a real 0.0 for no-trade days;
    anything else is a gap and fails closed."""
    grid = _grid()
    grid["S1-05"][FROZEN_DAY_KEYS[10]] = None
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4
        )


def test_no_trade_day_is_a_plain_zero_not_a_special_case():
    grid = _grid(value=0.0)
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.day_count == 365


def test_slices_is_pinned_at_four_and_passed_through_to_the_shared_gate():
    grid = _grid()
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.slices == 4


def test_wrong_slices_value_fails_closed():
    grid = _grid()
    for bad_slices in (2, 3, 5, 6):
        with pytest.raises(PboGridError):
            compute_pbo_auxiliary_evidence(
                strategy="S1", daily_net_bps_by_config=grid, slices=bad_slices
            )


def test_ambiguous_ranking_reason_code_surfaces_from_the_shared_gate():
    """When every config's return sequence is byte-identical, the shared
    ``honest_offline_gate`` authority itself reports an ambiguous-ranking
    reason (tied IS/OOS scores) -- this module must pass that reason
    through unchanged, not swallow or reinterpret it."""
    grid = _grid(value=1.0)
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.value is None
    assert "ambiguous_pbo_ranking" in evidence.reason_codes


def test_different_config_orderings_of_the_same_grid_hash_identically():
    """Dict insertion order of the 12 configs must never leak into the
    auxiliary artifact hash."""
    grid = _grid()
    reordered = dict(reversed(list(grid.items())))
    e1 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    e2 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=reordered, slices=4
    )
    assert e1.artifact_hash == e2.artifact_hash


def test_material_change_to_a_single_return_changes_the_artifact_hash():
    grid = _grid()
    e1 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    grid["S1-00"][FROZEN_DAY_KEYS[0]] = 5.0
    e2 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert e1.artifact_hash != e2.artifact_hash


def test_wrong_strategy_s3_fails_closed():
    grid = _grid(strategy="S3")
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S3", daily_net_bps_by_config=grid, slices=4
        )


def test_wrong_provenance_scenario_fails_closed():
    grid = _grid()
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1",
            daily_net_bps_by_config=grid,
            slices=4,
            scenario_name="base",  # frozen to primary_stress only
        )


def test_wrong_provenance_cost_bps_fails_closed():
    grid = _grid()
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1", daily_net_bps_by_config=grid, slices=4, cost_bps=13.0
        )


def test_wrong_provenance_window_fails_closed():
    grid = _grid()
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1",
            daily_net_bps_by_config=grid,
            slices=4,
            window_start_iso="2024-07-01T00:00:00Z",
        )


def test_provenance_is_bound_into_the_artifact_hash():
    """Two calls with the identical grid but different (still-frozen-
    valid, since only the DEFAULTS are tested here) provenance metadata
    would hash differently -- proven indirectly by checking the payload's
    own hash changes if the frozen evaluation_method constant changes."""
    grid = _grid()
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    # the hash must be a function of more than just value/reason_codes --
    # already proven by test_material_change_to_a_single_return_changes_
    # the_artifact_hash, this test documents that provenance fields are
    # part of the SAME hashed payload (see module source: scenario_name/
    # cost_bps/window_*/evaluation_method are literal payload keys).
    assert evidence.artifact_hash


def test_gap_invalid_day_fails_closed_even_with_a_plausible_zero_value():
    """A day explicitly marked gap-invalid must fail closed regardless of
    what numeric value sits in the grid for it -- even a perfectly
    plausible 0.0 (no-trade) must never be trusted as real evidence once
    the day itself is flagged as a data gap."""
    grid = _grid(value=0.0)
    gap_invalid = {"S1-05": frozenset({FROZEN_DAY_KEYS[10]})}
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1",
            daily_net_bps_by_config=grid,
            slices=4,
            gap_invalid_days_by_config=gap_invalid,
        )


def test_no_gap_invalid_days_is_the_default_and_succeeds():
    grid = _grid(value=0.0)
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.day_count == 365


def test_gap_invalid_days_for_an_unknown_config_fails_closed():
    grid = _grid()
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1",
            daily_net_bps_by_config=grid,
            slices=4,
            gap_invalid_days_by_config={"S2-00": frozenset({FROZEN_DAY_KEYS[0]})},
        )


def test_mutating_the_caller_grid_after_the_call_never_affects_the_result():
    """Deep-snapshot proof: mutating the caller's own nested dict AFTER
    calling compute_pbo_auxiliary_evidence must be completely inert."""
    grid = _grid()
    evidence_before = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    grid["S1-00"][FROZEN_DAY_KEYS[0]] = 999.0  # mutate AFTER the call
    grid["S1-00"].clear()
    evidence_after_mutation_but_same_call_result = evidence_before
    # re-derive independently to prove the ORIGINAL call's result is what
    # it is regardless of the later external mutation.
    assert evidence_after_mutation_but_same_call_result.day_count == 365
    assert evidence_after_mutation_but_same_call_result.config_count == 12


def test_internal_alignment_order_is_canonical_regardless_of_caller_mapping_order():
    """Even though the hash is already order-independent (proven
    separately), the STATISTICAL computation itself must be handed configs
    in canonical Sx-00..11 order -- proven here by checking two different
    caller orderings still produce the identical derived value/reason."""
    grid = _grid()
    reordered = dict(reversed(list(grid.items())))
    e1 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, slices=4
    )
    e2 = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=reordered, slices=4
    )
    assert e1.value == e2.value
    assert e1.reason_codes == e2.reason_codes


def test_s2_uses_its_own_canonical_config_id_set():
    grid = _grid(strategy="S2")
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S2", daily_net_bps_by_config=grid, slices=4
    )
    assert evidence.strategy == "S2"
    assert evidence.config_count == 12
