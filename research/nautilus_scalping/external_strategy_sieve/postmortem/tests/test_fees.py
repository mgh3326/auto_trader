"""ROB-384 — fee-grid recompute tests."""

from __future__ import annotations

import math

from external_strategy_sieve.postmortem import fees


def test_endpoints_are_exact():
    assert fees.net_at_fee(100.0, -50.0, 0.0) == 100.0
    assert fees.net_at_fee(100.0, -50.0, 10.0) == -50.0


def test_midpoint_is_linear():
    # gross 100 at fee 0, net -50 at fee 10 -> at fee 5 the midpoint is 25.
    assert fees.net_at_fee(100.0, -50.0, 5.0) == 25.0


def test_matches_rob382_recorded_frozen_taker():
    # ichi: our_gross_bps=15.258 (fee 0), our_net_bps_retail_ref=-4.742 (fee 10).
    # The artifact independently recorded our_net_bps_frozen_taker=7.258 (fee 4).
    # The linear model must reproduce that recorded value.
    got = fees.net_at_fee(15.258, -4.742, 4.0)
    assert math.isclose(got, 7.258, abs_tol=1e-9)


def test_grid_keys_and_monotonicity():
    grid = fees.fee_grid(15.258, -4.742)
    assert set(grid) == {"0", "2", "4", "7.5", "10"}
    # Net decreases monotonically as fee rises (positive gross edge).
    seq = [grid["0"], grid["2"], grid["4"], grid["7.5"], grid["10"]]
    assert seq == sorted(seq, reverse=True)


def test_ref_fee_zero_rejected():
    try:
        fees.net_at_fee(1.0, 1.0, 1.0, ref_fee_bps=0.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for zero ref_fee")


def test_expectancy_to_bps_convention():
    # notional 1000: expectancy 0.55 -> 5.5 bps/trade (matches ROB-383 bbrsi).
    assert math.isclose(fees.expectancy_to_bps(0.55), 5.5, abs_tol=1e-9)


def test_fee_key_formatting():
    assert fees._fee_key(2.0) == "2"
    assert fees._fee_key(7.5) == "7.5"
