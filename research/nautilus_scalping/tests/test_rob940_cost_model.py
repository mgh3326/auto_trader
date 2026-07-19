"""ROB-942 (H2, ROB-940) — corrected cost model RED fixtures.

Pins the frozen ROB-940 cost contract (orch 2026-07-17 fee-recalibration
comment): taker 5bp/leg, round-trip fee 10bp, all-in scenarios 13/17/22bp
(primary=17bp), and the derived 68bp minimum-TP-distance gate (4x17bp). Also
pins the realized-funding-crossing sign convention and the "subtract exactly
once" net formula (no fee+all_in double count).
"""

import rob940_cost_model as cm


def test_fee_constants_are_5bp_legs_10bp_round_trip():
    assert cm.FEE_ENTRY_BPS == 5.0
    assert cm.FEE_EXIT_BPS == 5.0
    assert cm.FEE_ROUND_TRIP_BPS == 10.0


def test_cost_scenarios_are_13_17_22_with_17_primary():
    assert cm.COST_SCENARIO_BASE.all_in_bps == 13.0
    assert cm.COST_SCENARIO_PRIMARY_STRESS.all_in_bps == 17.0
    assert cm.COST_SCENARIO_UPWARD_STRESS.all_in_bps == 22.0
    assert [s.all_in_bps for s in cm.COST_SCENARIOS] == [13.0, 17.0, 22.0]


def test_min_tp_distance_is_4x_primary_stress_68bp():
    assert cm.MIN_TP_DISTANCE_BPS == 68.0
    assert cm.MIN_TP_DISTANCE_BPS == 4.0 * cm.COST_SCENARIO_PRIMARY_STRESS.all_in_bps


def test_gross_bps_long_and_short_symmetry():
    assert abs(cm.gross_bps("long", 100.0, 101.0) - 100.0) < 1e-9
    assert abs(cm.gross_bps("short", 100.0, 99.0) - 100.0) < 1e-9
    # entry-based: exact barrier distance realizes exact bps magnitude, both sides
    assert cm.gross_bps("long", 100.0, 101.0) == -cm.gross_bps("short", 100.0, 101.0)
    # mirrored move, mirrored side -> same-sign gain
    long_gain = cm.gross_bps("long", 100.0, 105.0)
    short_gain = cm.gross_bps("short", 100.0, 95.0)
    assert long_gain > 0 and short_gain > 0


def test_gross_bps_rejects_nonpositive_entry():
    try:
        cm.gross_bps("long", 0.0, 10.0)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_realized_funding_bps_sign_convention_long_pays_short_receives():
    crossings = [
        cm.FundingCrossing(ts=1, rate_bps=2.0),
        cm.FundingCrossing(ts=2, rate_bps=1.0),
    ]
    assert cm.realized_funding_bps("long", crossings) == 3.0
    assert cm.realized_funding_bps("short", crossings) == -3.0


def test_realized_funding_bps_no_crossings_is_exact_float_zero():
    # ROB-962: `0 == 0.0` in Python, so a bare `== 0.0` assertion previously
    # passed even when the empty-sum authority returned plain `int 0` --
    # H5's compute_scenario_metrics requires an exact `float` (type(x) is
    # float, never isinstance), so the RUNTIME TYPE must be pinned here too.
    long_value = cm.realized_funding_bps("long", [])
    assert type(long_value) is float
    assert long_value.hex() == (0.0).hex()

    # short negates the (empty) sum -- 0.0 negates to -0.0, a distinct bit
    # pattern but still an exact float zero (`type(x) is float`, `x == 0.0`).
    short_value = cm.realized_funding_bps("short", [])
    assert type(short_value) is float
    assert short_value == 0.0
    assert short_value.hex() in ((0.0).hex(), (-0.0).hex())


def test_realized_funding_bps_nonempty_crossings_still_exact_float():
    # Non-empty-crossing sums must remain exact float (unchanged sign/value
    # behavior) alongside the empty-crossing fix above.
    crossings = [
        cm.FundingCrossing(ts=1, rate_bps=2.0),
        cm.FundingCrossing(ts=2, rate_bps=1.0),
    ]
    long_value = cm.realized_funding_bps("long", crossings)
    short_value = cm.realized_funding_bps("short", crossings)
    assert type(long_value) is float
    assert type(short_value) is float
    assert long_value == 3.0
    assert short_value == -3.0


def test_net_bps_subtracts_all_in_and_funding_exactly_once_no_fee_double_count():
    # gross=100, scenario all_in=17 (already embeds the 10bp fee), funding=3
    net = cm.net_bps(100.0, cm.COST_SCENARIO_PRIMARY_STRESS, 3.0)
    assert net == 100.0 - 17.0 - 3.0
    # fee_bps (10.0) must NOT appear as an additional subtraction anywhere
    assert net != 100.0 - cm.FEE_ROUND_TRIP_BPS - 17.0 - 3.0


def test_net_bps_scenario_sensitivity_same_gross_and_funding():
    gross, funding = 50.0, 0.0
    nets = [cm.net_bps(gross, s, funding) for s in cm.COST_SCENARIOS]
    assert nets == [50.0 - 13.0, 50.0 - 17.0, 50.0 - 22.0]
    assert nets[0] > nets[1] > nets[2]
