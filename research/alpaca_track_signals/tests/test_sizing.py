from __future__ import annotations

import artifact as art
import identity
import pytest
import seal_consumption as sc
import sizing


def test_ap_a1_base_slot_is_62_50():
    assert sizing.ap_a1_base_slot_usd() == pytest.approx(62.50)


def test_available_cash_and_base_slot_track_a_reseal_of_equity_and_base_slot(
    monkeypatch,
):
    """ROB-1061 adversarial-verification Finding (AC18 re-declaration):
    ``sizing.py`` used to hardcode its own copy of the $2,000 initial equity
    and $62.50 AP-A1 base slot, on the (false) claim that H2's seal never
    captures them. It does -- ``identity._build_frozen_config_component``
    embeds both, folded into ``SEALED_ARTIFACT_SEMANTIC_HASH``. Simulate the
    exact authorized re-seal the adversarial verifier used (equity ->
    $1,600, base_slot -> $50.00) and prove ``sizing.py``'s numbers now move
    WITH the seal instead of silently diverging at the old hardcoded
    2000.0/62.50."""
    original = identity._build_frozen_config_component

    def _resealed(config, seal):
        component = dict(original(config, seal))
        component["initial_equity_usd"] = 1600
        component["base_slot_usd"] = 50.0
        return component

    monkeypatch.setattr(identity, "_build_frozen_config_component", _resealed)
    # Simulate an AUTHORIZED re-seal: re-pin SEALED_ARTIFACT_SEMANTIC_HASH to
    # match the freshly-rebuilt (resealed) artifact -- otherwise
    # load_sealed_configs_and_params() correctly fails closed on its OWN
    # drift check before sizing.py ever sees the new value (exactly as it
    # should for an UNAUTHORIZED drift; this test is about an authorized one).
    resealed_artifact = art.build_sealed_artifact()
    monkeypatch.setattr(
        art, "SEALED_ARTIFACT_SEMANTIC_HASH", resealed_artifact.semantic_hash()
    )
    assert sc.initial_equity_usd() == pytest.approx(1600.0)
    assert sizing.available_cash({}) == pytest.approx(1600.0)
    assert sizing.ap_a1_base_slot_usd() == pytest.approx(50.0)
    assert sizing.ap_a2_base_slot_usd(5) == pytest.approx(1600.0 / 5)


def test_vol_scale_clamps_at_1_0_never_exceeds_it():
    # sigma20 very small -> 0.50/sigma20 would be huge without the clamp.
    assert sizing.compute_vol_scale(0.01) == 1.0
    assert sizing.compute_vol_scale(0.0001) == 1.0


def test_vol_scale_is_the_literal_ratio_when_below_1():
    sigma20 = 1.0  # 0.50/1.0 = 0.50
    assert sizing.compute_vol_scale(sigma20) == pytest.approx(0.50)


def test_vol_scale_rejects_non_positive_sigma():
    with pytest.raises(ValueError, match="positive"):
        sizing.compute_vol_scale(0.0)


def test_target_notional_ap_a1_is_base_slot_times_vol_scale():
    assert sizing.target_notional_ap_a1(0.5) == pytest.approx(62.50 * 0.5)


def test_min_target_notional_boundary_is_read_from_the_seal():
    floor = sc.min_strategy_target_usd()
    assert sizing.meets_min_target_notional(floor) is True
    assert sizing.meets_min_target_notional(floor - 0.01) is False


def test_ap_a2_target_notional_is_capped_by_available_cash():
    # base_slot(k=5) * vol_scale=1.0 -> 2000/5 = 400, but only $30 cash left.
    result = sizing.target_notional_ap_a2(k=5, vol_scale=1.0, available_cash=30.0)
    assert result == pytest.approx(30.0)


def test_ap_a2_target_notional_uses_formula_when_cash_is_not_binding():
    result = sizing.target_notional_ap_a2(k=5, vol_scale=1.0, available_cash=10_000.0)
    assert result == pytest.approx(400.0)


def test_available_cash_subtracts_committed_notional_from_fixed_equity():
    committed = {"BTC/USD": 100.0, "ETH/USD": 50.0}
    # SS11.5/SS12.5's literal preregistered value ($2,000), NOT
    # `sizing`'s own internals -- an independent oracle, not a tautology.
    assert sizing.available_cash(committed) == pytest.approx(2000.0 - 150.0)


def test_available_cash_with_no_open_positions_is_full_equity():
    assert sizing.available_cash({}) == pytest.approx(2000.0)


# --------------------------------------------------------------------------- #
# allocate_cash_constrained — AC12: D descending, symbol ascending tie-break
# --------------------------------------------------------------------------- #


def test_allocation_orders_by_d_descending():
    candidates = [
        ("AAA/USD", 0.01, 100.0),
        ("BBB/USD", 0.03, 100.0),
        ("CCC/USD", 0.02, 100.0),
    ]
    outcome = sizing.allocate_cash_constrained(candidates, available_cash=1000.0)
    assert outcome.accepted == ("BBB/USD", "CCC/USD", "AAA/USD")


def test_allocation_tie_break_is_symbol_ascending_not_insertion_order():
    candidates = [
        ("ZZZ/USD", 0.05, 100.0),
        ("AAA/USD", 0.05, 100.0),
        ("MMM/USD", 0.05, 100.0),
    ]
    outcome = sizing.allocate_cash_constrained(candidates, available_cash=1000.0)
    assert outcome.accepted == ("AAA/USD", "MMM/USD", "ZZZ/USD")


def test_allocation_rejects_candidates_that_do_not_fit_remaining_cash():
    candidates = [
        ("AAA/USD", 0.05, 80.0),
        ("BBB/USD", 0.03, 80.0),
        ("CCC/USD", 0.01, 80.0),
    ]
    outcome = sizing.allocate_cash_constrained(candidates, available_cash=150.0)
    # AAA (highest D) fits (80 <= 150, remaining 70). BBB needs 80 > 70 ->
    # rejected. CCC needs 80 > 70 -> rejected too (remaining unchanged after
    # a rejection -- a rejected candidate never consumes cash).
    assert outcome.accepted == ("AAA/USD",)
    assert outcome.rejected_insufficient_cash == ("BBB/USD", "CCC/USD")
    assert outcome.remaining_cash == pytest.approx(70.0)


def test_allocation_with_no_candidates_is_a_no_op():
    outcome = sizing.allocate_cash_constrained([], available_cash=500.0)
    assert outcome.accepted == ()
    assert outcome.rejected_insufficient_cash == ()
    assert outcome.remaining_cash == 500.0


def test_allocation_uses_greedy_continue_not_stop_on_first_rejection():
    # THIRD UNDOCUMENTED OPEN CHOICE (now documented, see
    # allocate_cash_constrained's docstring): §11.5 specifies an ORDER (D
    # descending, symbol ascending), not a stop rule. AAA (highest D) does
    # NOT fit $100 cash and is rejected; BBB (lower D, smaller notional)
    # STILL gets funded from the full, untouched $100 -- a rejected
    # candidate never consumes cash, so allocation continues past it rather
    # than stopping.
    candidates = [("AAA/USD", 0.09, 900.0), ("BBB/USD", 0.02, 50.0)]
    outcome = sizing.allocate_cash_constrained(candidates, available_cash=100.0)
    assert outcome.accepted == ("BBB/USD",)
    assert outcome.rejected_insufficient_cash == ("AAA/USD",)
    assert outcome.remaining_cash == pytest.approx(50.0)
