"""ROB-1060 H2 — RED-first: the 4 sealed execution parameters + cost
scenarios + gate thresholds, literal (no rounding/approximation/
reinterpretation) per the operator decision comment and Run A SS11.7/12.6/12.7/17.
"""

from __future__ import annotations

import pytest

# --------------------------------------------------------------------------- #
# 1. Eligible universe + PEPE/SHIB operator-decision exclusion                 #
# --------------------------------------------------------------------------- #


def test_universe_seal_literal_exclusion_fields_match_the_operator_decision():
    import params as m

    sealed = m.build_sealed_params()
    u = sealed.universe
    assert u.excluded_symbols == ("PEPE", "SHIB")
    assert u.exclusion_reason == "basis_red_grade_hv_p95_ge_28bp"
    assert u.exclusion_authority == "operator_decision_2026-07-25"
    assert u.sealed_effective_n == 20


def test_universe_seal_raw_n_today_is_22_and_ge18_invariant_holds():
    import params as m

    u = m.build_sealed_params().universe
    assert u.n_raw_today == 22
    assert u.n_raw_today >= 18
    assert u.sealed_effective_n >= 18


def test_sealed_symbol_list_excludes_pepe_and_shib_but_raw_list_includes_them():
    import params as m

    u = m.build_sealed_params().universe
    raw_symbols = {s.alpaca_symbol for s in u.raw_symbols}
    sealed_symbols = {s.alpaca_symbol for s in u.sealed_symbols}
    assert "PEPE/USD" in raw_symbols
    assert "SHIB/USD" in raw_symbols
    assert "PEPE/USD" not in sealed_symbols
    assert "SHIB/USD" not in sealed_symbols
    assert len(raw_symbols) == 22
    assert len(sealed_symbols) == 20
    assert sealed_symbols == raw_symbols - {"PEPE/USD", "SHIB/USD"}


def test_alpaca_first_daily_is_marked_as_a_pit_proxy_for_every_symbol():
    import params as m

    u = m.build_sealed_params().universe
    for entry in u.raw_symbols:
        assert entry.alpaca_first_daily_is_pit_proxy is True


def test_btc_quote_mode_is_direct_usdc_and_bat_is_usdt_proxy():
    import params as m

    u = m.build_sealed_params().universe
    by_symbol = {s.alpaca_symbol: s for s in u.raw_symbols}
    assert by_symbol["BTC/USD"].quote_mode == "USDC"
    assert by_symbol["BAT/USD"].quote_mode == "USDT_PROXY"
    assert by_symbol["AAVE/USD"].quote_mode == "SYNTH_USDC"


# --------------------------------------------------------------------------- #
# 2. Spread census                                                             #
# --------------------------------------------------------------------------- #


def test_spread_census_median_of_medians_literal_values():
    import params as m

    sc = m.build_sealed_params().spread_census
    assert sc.median_of_medians_all_bp == pytest.approx(36.35)
    assert sc.median_of_medians_eligible_bp == pytest.approx(30.12)


def test_spread_census_cost_heterogeneity_symbols_and_medians():
    import params as m

    sc = m.build_sealed_params().spread_census
    by_symbol = {e.alpaca_symbol: e.median_bp for e in sc.cost_heterogeneity_symbols}
    assert set(by_symbol) == {"BAT/USD", "BCH/USD", "LTC/USD", "XTZ/USD", "AVAX/USD"}
    assert by_symbol["BAT/USD"] == pytest.approx(58.58)
    assert by_symbol["BCH/USD"] == pytest.approx(58.59)
    assert by_symbol["LTC/USD"] == pytest.approx(58.84)
    assert by_symbol["XTZ/USD"] == pytest.approx(60.16)
    assert by_symbol["AVAX/USD"] == pytest.approx(61.25)
    for median in by_symbol.values():
        assert 58 <= median <= 65


# --------------------------------------------------------------------------- #
# 3. Paper fee — measured, NOT "confirmed" (ROB-1066 CFEE format still pending)#
# --------------------------------------------------------------------------- #


def test_paper_fee_is_25_bp_coin_side_and_manual_deduction_forbidden():
    import params as m

    fee = m.build_sealed_params().paper_fee
    assert fee.paper_fee_bp == pytest.approx(25.0)
    assert fee.manual_fee_deduction == "FORBIDDEN"


def test_paper_fee_is_not_claimed_fully_confirmed_pending_rob_1066():
    """Precision matters more than looking finished: the measured 25.0bp is
    sealed, but the end-of-day CFEE activity posting format re-verification
    (ROB-1066) had not closed as of the probe's UTC day. The seal must say so
    explicitly, not silently claim full confirmation."""
    import params as m

    fee = m.build_sealed_params().paper_fee
    assert fee.confirmed_end_of_day_posting_format is False
    assert "ROB-1066" in fee.provenance_note
    assert "pending" in fee.provenance_note.lower()


# --------------------------------------------------------------------------- #
# 4. Frozen basis cap                                                          #
# --------------------------------------------------------------------------- #


def test_frozen_basis_cap_raw_table_has_22_symbols_incl_pepe_shib():
    import params as m

    cap = m.build_sealed_params().frozen_basis_cap
    assert len(cap.raw_cap_bp) == 22
    assert cap.raw_cap_bp["PEPE/USD"] == 55
    assert cap.raw_cap_bp["SHIB/USD"] == 32
    assert cap.raw_cap_bp["BTC/USD"] == 11


def test_frozen_basis_cap_sealed_table_excludes_pepe_shib_20_symbols():
    import params as m

    cap = m.build_sealed_params().frozen_basis_cap
    assert len(cap.sealed_cap_bp) == 20
    assert "PEPE/USD" not in cap.sealed_cap_bp
    assert "SHIB/USD" not in cap.sealed_cap_bp
    assert cap.sealed_cap_bp["BTC/USD"] == 11
    assert cap.sealed_cap_bp["YFI/USD"] == 27


def test_frozen_basis_cap_is_close_basis_proxy_not_executable():
    import params as m

    cap = m.build_sealed_params().frozen_basis_cap
    assert "close-basis" in cap.proxy_note.lower()
    assert "rob-1067" in cap.proxy_note.lower() or "ROB-1067" in cap.proxy_note


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 5: only 2 of 20 basis caps were pinned (BTC=11,       #
# YFI=27) -- AAVE 14 -> 15 (and any other single-symbol drift) survived a     #
# green suite. Pin all 20 literally, AND independently recompute each from    #
# the raw fixture's `venue_basis.high_vol.abs_p95_bp` via the documented      #
# `ceil(hv_p95_bp) + 3` method -- so a wrong value in the fixture itself, not #
# just a wrong transcription, is caught too.                                  #
# --------------------------------------------------------------------------- #


def test_all_20_sealed_basis_caps_match_the_pinned_literal_table():
    import params as m

    cap = m.build_sealed_params().frozen_basis_cap
    assert dict(cap.sealed_cap_bp) == {
        "BTC/USD": 11,
        "ETH/USD": 13,
        "SOL/USD": 16,
        "DOGE/USD": 13,
        "XRP/USD": 13,
        "AAVE/USD": 14,
        "AVAX/USD": 18,
        "BAT/USD": 23,
        "BCH/USD": 14,
        "CRV/USD": 17,
        "DOT/USD": 19,
        "GRT/USD": 20,
        "LINK/USD": 12,
        "LTC/USD": 14,
        "SKY/USD": 17,
        "SUSHI/USD": 20,
        "TRUMP/USD": 16,
        "UNI/USD": 15,
        "XTZ/USD": 17,
        "YFI/USD": 27,
    }


def test_all_22_raw_basis_caps_match_ceil_hv_p95_plus_3_recomputed_from_source():
    """Recomputes the cap independently from the raw fixture data (NOT from
    `build_sealed_params()`'s output) so a fixture-level or method-level
    error, not just a copy-paste error, is caught."""
    import math

    import source_provenance as sp

    raw = sp.load_basis_analysis_full()
    caps = raw["_frozen_basis_cap_proposal_bp"]
    assert len(caps) == 22
    for symbol, cap in caps.items():
        p95 = raw[symbol]["venue_basis"]["high_vol"]["abs_p95_bp"]
        expected = math.ceil(p95) + 3
        assert cap == expected, (
            f"{symbol}: sealed cap {cap} != ceil({p95}) + 3 == {expected}"
        )


def test_frozen_basis_cap_method_string_is_the_literal_ceil_plus_3_description():
    import params as m

    cap = m.build_sealed_params().frozen_basis_cap
    assert cap.method == "ceil(hv_p95_bp) + 3bp margin (Run B SS10)"


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 7: dict-valued sealed fields must be genuinely        #
# immutable through the object -- `frozen=True` blocks attribute rebinding    #
# but not `sealed.raw_cap_bp["X"] = ...` mutating the dict in place.          #
# --------------------------------------------------------------------------- #


def test_frozen_basis_cap_raw_cap_bp_is_immutable_through_the_object():
    import params as m

    sealed = m.build_sealed_params()
    with pytest.raises(TypeError, match="does not support item assignment"):
        sealed.frozen_basis_cap.raw_cap_bp["BTC/USD"] = 999


def test_frozen_basis_cap_sealed_cap_bp_is_immutable_through_the_object():
    import params as m

    sealed = m.build_sealed_params()
    with pytest.raises(TypeError, match="does not support item assignment"):
        sealed.frozen_basis_cap.sealed_cap_bp["BTC/USD"] = 999


def test_cost_scenarios_scenarios_bp_is_immutable_through_the_object():
    import params as m

    sealed = m.build_sealed_params()
    with pytest.raises(TypeError, match="does not support item assignment"):
        sealed.cost_scenarios.scenarios_bp["C200"] = 200


# --------------------------------------------------------------------------- #
# 5. Cost scenarios — exactly 4, C50/C100/C120/C150                           #
# --------------------------------------------------------------------------- #


def test_cost_scenarios_are_exactly_c50_c100_c120_c150():
    import params as m

    cs = m.build_sealed_params().cost_scenarios
    assert cs.scenarios_bp == {"C50": 50, "C100": 100, "C120": 120, "C150": 150}
    assert cs.primary == "C120"
    assert cs.upward == "C150"


def test_validate_cost_scenarios_rejects_a_5th_scenario():
    import params as m

    tampered = dict(m.build_sealed_params().cost_scenarios.scenarios_bp)
    tampered["C0"] = 0
    with pytest.raises(m.CostScenarioCountError, match="4"):
        m.validate_cost_scenarios(tampered)


def test_validate_cost_scenarios_rejects_wrong_scenario_names():
    import params as m

    tampered = {"C50": 50, "C100": 100, "C120": 120, "C200": 200}
    with pytest.raises(m.CostScenarioNameError, match="expected exactly"):
        m.validate_cost_scenarios(tampered)


# --------------------------------------------------------------------------- #
# 6. Gate thresholds — SS11.7 / SS12.7 / common / turnover band, verbatim      #
# --------------------------------------------------------------------------- #


def test_common_gate_literals():
    import params as m

    g = m.build_sealed_params().gate_thresholds
    assert g.min_modeled_entries_per_fold == 5
    assert g.fixed_tp == "NONE"
    assert g.future_tp_min_bp == 240


def test_ap_a1_gate_has_the_11_conditions_from_section_11_7():
    """ROB-1060 H2-lock item 2: pins EVERY field of EVERY condition --
    `.op` was previously asserted only for `all_folds_entries`, so an
    inverted operator elsewhere (`max_oos_dd_pct <= -> >=`,
    `pooled_gross_ev_bp >= -> <=`, `e150 > 0 -> < 0`) survived a green
    suite. Also pins `e120_bootstrap_95_lower_bound`'s threshold literally
    (`> 0` relaxed to `> -50` also survived)."""
    import params as m

    g = m.build_sealed_params().gate_thresholds
    assert len(g.ap_a1) == 11
    actual = [(c.metric, c.op, c.value, c.unit) for c in g.ap_a1]
    expected = [
        ("all_folds_entries", ">=", 5, "count"),
        ("median_hold_days", ">=", 3, "days"),
        ("pooled_gross_ev_bp", ">=", 180, "bp"),
        ("pooled_e120_bp", ">=", 60, "bp"),
        ("e120_bootstrap_95_lower_bound", ">", 0, "bp"),
        ("e150", ">", 0, "bp"),
        ("pf120", ">=", pytest.approx(1.15), None),
        ("positive_folds", ">=", (5, 8), "folds_of_8"),
        ("max_oos_dd_pct", "<=", 20, "pct"),
        ("monthly_concentration_pct", "<=", 50, "pct"),
        ("symbol_concentration_pct", "<=", 40, "pct"),
    ]
    assert actual == expected


def test_ap_a2_gate_has_the_13_conditions_literally_present_in_section_12_7():
    """SPEC NOTE: ROB-1060 AC12 labels this "12항" (12 items), but the Run A
    authority doc SS12.7 literally enumerates 13 distinct gate conditions (one
    more than AP-A1's 11, not the same "off-by-one-less" the AC text implies).
    Per the hard constraint "do not round/approximate/reinterpret any
    threshold — if a value looks self-contradictory, STOP and report", this
    seals the AUTHORITY DOC's literal 13 conditions verbatim and does not
    silently drop one to match the AC's item-count label. See H2 completion
    report for the flagged discrepancy."""
    import params as m

    g = m.build_sealed_params().gate_thresholds
    assert len(g.ap_a2) == 13
    actual = [(c.metric, c.op, c.value, c.unit) for c in g.ap_a2]
    expected = [
        ("all_folds_entries", ">=", 5, "count"),
        ("turnover_in_intersection", "==", True, None),
        ("pooled_gross_ev_bp", ">=", 200, "bp"),
        ("pooled_e120_bp", ">=", 80, "bp"),
        ("e120_bootstrap_lower_bound", ">", 0, "bp"),
        ("e150", ">", 0, "bp"),
        ("pf120", ">=", pytest.approx(1.15), None),
        ("positive_folds", ">=", (5, 8), "folds_of_8"),
        ("equal_weight_e120_positive", "==", True, None),
        ("topk_vs_middle_wins", ">=", (5, 8), "folds_of_8"),
        ("max_oos_dd_pct", "<=", 20, "pct"),
        ("monthly_concentration_pct", "<=", 50, "pct"),
        ("symbol_concentration_pct", "<=", 35, "pct"),
    ]
    assert actual == expected


def test_ap_a2_turnover_band_is_exactly_20_83_to_28_85_pct():
    import params as m

    g = m.build_sealed_params().gate_thresholds
    lower, upper = g.ap_a2_turnover_band
    assert lower == pytest.approx(0.2083)
    assert upper == pytest.approx(0.2885)


# --------------------------------------------------------------------------- #
# 7. SS17 final status block literals                                         #
# --------------------------------------------------------------------------- #


def test_run_status_block_literals():
    import params as m

    r = m.build_sealed_params().run_status
    assert r.total_configs == 16
    assert r.oos_folds == 8
    assert r.oos_days == 28
    assert r.order_type == "LIMIT_ONLY"
    assert r.economic_execution == "TAKER_TAKER"
    assert r.min_broker_order_usd == 10
    assert r.min_strategy_target_usd == 25
    assert r.no_threshold_relaxation is True
    assert r.no_post_pnl_config_addition is True


def test_build_sealed_params_is_reproducible_across_calls():
    import params as m

    first = m.build_sealed_params()
    second = m.build_sealed_params()
    assert first == second
