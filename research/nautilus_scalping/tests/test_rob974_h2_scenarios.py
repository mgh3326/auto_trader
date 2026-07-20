"""ROB-979 (H2, ROB-974 R2) CP4 -- scenarios, funding, provenance, deterministic
ledgers (RED first).

Covers ROB-979 AC27-34: explicit base13/primary_stress17/upward_stress22 path
scenarios with fresh independent state (no hidden shared-ledger revaluation),
every row carrying gross_bps plus all three E13/E17/E22 counterfactual
columns, PIT [entry,exit) realized funding (S3 signed single-leg, S4
entry-frozen weighted signed two-leg applied once on basket notional),
thesis_exit_flag/timeout_flag derivation, canonical ledger ordering/hashing
independent of input order, exact float-zero funding, unwrapped exception
propagation, and the absence of any cost-0/fourth-scenario API. See
``rob974_h2_scenarios.py`` module docstring for the ultrathink design log.
"""

from __future__ import annotations

import pytest
from rob940_cost_model import FundingCrossing
from rob974_h2_dtos import S3Trade, S4PairTrade
from rob974_h2_scenarios import (
    PATH_SCENARIO_BASE13,
    PATH_SCENARIO_PRIMARY_STRESS17,
    PATH_SCENARIO_UPWARD_STRESS22,
    PATH_SCENARIOS,
    build_s3_scenario_ledger,
    build_s4_scenario_ledger,
    s3_ledger_hash,
    s4_ledger_hash,
)


def _s3_trade(**overrides):
    fields = {
        "symbol": "XRPUSDT",
        "side": "long",
        "config_id": "s3-00",
        "fold_id": "fold-00",
        "signal_ts": 0,
        "entry_ts": 0,
        "entry_price": 1.0,
        "exit_ts": 60_000,
        "exit_price": 1.02,
        "exit_reason": "TP",
        "mfe_bps": 200.0,
        "mae_bps": 0.0,
        "gross_bps": 200.0,
        "volatility_percentile": 55.0,
    }
    fields.update(overrides)
    return S3Trade(**fields)


def _s4_trade(**overrides):
    fields = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "side_a": "short",
        "side_b": "long",
        "config_id": "s4-00",
        "fold_id": "fold-00",
        "signal_ts": 0,
        "entry_ts": 0,
        "weight_a": 0.4,
        "weight_b": 0.6,
        "entry_price_a": 1.0,
        "entry_price_b": 1.0,
        "exit_ts": 60_000,
        "exit_price_a": 0.99,
        "exit_price_b": 1.01,
        "exit_reason": "TP",
        "mfe_bps": 150.0,
        "mae_bps": 0.0,
        "gross_bps": 150.0,
        "order_id_a": None,
        "order_id_b": None,
        "pair_exec_status": "historical_atomic_assumption",
        "pair_executor_validated": False,
        "demo_eligible": False,
        "volatility_percentile": None,
        "volatility_percentile_provenance": "not_defined_for_s4",
    }
    fields.update(overrides)
    return S4PairTrade(**fields)


class TestPathScenarioIdentifiers:
    def test_exactly_three_labels(self):
        assert PATH_SCENARIOS == (
            PATH_SCENARIO_BASE13,
            PATH_SCENARIO_PRIMARY_STRESS17,
            PATH_SCENARIO_UPWARD_STRESS22,
        )
        assert len(PATH_SCENARIOS) == 3

    def test_no_cost_zero_or_fourth_scenario_api(self):
        import rob974_h2_scenarios as mod

        names = dir(mod)
        assert not any("zero" in n.lower() for n in names)
        assert not any(
            n.lower() in ("e0", "cost_scenario_zero", "e0_bps") for n in names
        )


class TestS3ScenarioLedger:
    def test_e13_e17_e22_all_present_from_single_subtraction(self):
        trade = _s3_trade(gross_bps=200.0)
        rows = build_s3_scenario_ledger([trade], PATH_SCENARIO_BASE13)
        row = rows[0]
        assert row.path_scenario == PATH_SCENARIO_BASE13
        assert abs(row.e13_bps - (200.0 - 13.0)) < 1e-9
        assert abs(row.e17_bps - (200.0 - 17.0)) < 1e-9
        assert abs(row.e22_bps - (200.0 - 22.0)) < 1e-9
        assert row.trade.gross_bps == 200.0  # price-only gross preserved unchanged

    def test_funding_subtracted_exactly_once(self):
        trade = _s3_trade(gross_bps=200.0, entry_ts=0, exit_ts=120_000, side="long")

        def lookup(symbol, side, entry_ts, exit_ts):
            return (FundingCrossing(ts=60_000, rate_bps=5.0),)

        rows = build_s3_scenario_ledger([trade], PATH_SCENARIO_PRIMARY_STRESS17, lookup)
        row = rows[0]
        assert abs(row.funding_bps - 5.0) < 1e-9
        assert abs(row.e17_bps - (200.0 - 17.0 - 5.0)) < 1e-9

    def test_exact_float_zero_funding_with_no_lookup(self):
        trade = _s3_trade()
        rows = build_s3_scenario_ledger([trade], PATH_SCENARIO_BASE13)
        assert type(rows[0].funding_bps) is float
        assert rows[0].funding_bps == 0.0

    def test_thesis_exit_flag_only_for_thesis_exit(self):
        thesis = _s3_trade(exit_reason="THESIS_EXIT")
        tp = _s3_trade(exit_reason="TP")
        timeout = _s3_trade(exit_reason="TIMEOUT")
        rows = build_s3_scenario_ledger([thesis, tp, timeout], PATH_SCENARIO_BASE13)
        flags = {
            r.trade.exit_reason: (r.thesis_exit_flag, r.timeout_flag) for r in rows
        }
        assert flags["THESIS_EXIT"] == (True, False)
        assert flags["TP"] == (False, False)
        assert flags["TIMEOUT"] == (False, True)


class TestS4ScenarioLedger:
    def test_weighted_two_leg_funding_applied_once(self):
        trade = _s4_trade(weight_a=0.4, weight_b=0.6, entry_ts=0, exit_ts=60_000)

        def lookup(symbol, side, entry_ts, exit_ts):
            if symbol == "XRPUSDT":
                return (FundingCrossing(ts=30_000, rate_bps=10.0),)
            return (FundingCrossing(ts=30_000, rate_bps=4.0),)

        rows = build_s4_scenario_ledger([trade], PATH_SCENARIO_BASE13, lookup)
        row = rows[0]
        # side_a=short -> leg funding = -10.0; side_b=long -> leg funding = +4.0
        expected = 0.4 * (-10.0) + 0.6 * (4.0)
        assert abs(row.funding_bps - expected) < 1e-9
        assert abs(row.e13_bps - (150.0 - 13.0 - expected)) < 1e-9

    def test_mean_and_stall_exit_set_thesis_exit_flag(self):
        mean = _s4_trade(exit_reason="MEAN_EXIT")
        stall = _s4_trade(exit_reason="STALL_EXIT")
        tp = _s4_trade(exit_reason="TP")
        rows = build_s4_scenario_ledger([mean, stall, tp], PATH_SCENARIO_BASE13)
        flags = {r.trade.exit_reason: r.thesis_exit_flag for r in rows}
        assert flags["MEAN_EXIT"] is True
        assert flags["STALL_EXIT"] is True
        assert flags["TP"] is False

    def test_row_preserves_historical_null_posture_from_embedded_trade(self):
        trade = _s4_trade()
        rows = build_s4_scenario_ledger([trade], PATH_SCENARIO_BASE13)
        row = rows[0]
        assert row.trade.demo_eligible is False
        assert row.trade.volatility_percentile is None
        assert row.trade.volatility_percentile_provenance == "not_defined_for_s4"


class TestFreshIndependentState:
    def test_two_calls_never_share_mutable_state(self):
        trade1 = _s3_trade(gross_bps=100.0)
        trade2 = _s3_trade(gross_bps=999.0, signal_ts=999)
        rows_a = build_s3_scenario_ledger([trade1], PATH_SCENARIO_BASE13)
        rows_b = build_s3_scenario_ledger([trade2], PATH_SCENARIO_PRIMARY_STRESS17)
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0].trade.gross_bps == 100.0  # unaffected by the second call
        assert rows_a[0].path_scenario == PATH_SCENARIO_BASE13
        assert rows_b[0].path_scenario == PATH_SCENARIO_PRIMARY_STRESS17

    def test_membership_can_legitimately_diverge_across_scenario_calls(self):
        # H4 may feed genuinely different raw membership per scenario run;
        # H2 must never assume/enforce equal counts across ledger calls.
        rows_a = build_s3_scenario_ledger([_s3_trade()], PATH_SCENARIO_BASE13)
        rows_b = build_s3_scenario_ledger(
            [_s3_trade(), _s3_trade(signal_ts=1)], PATH_SCENARIO_UPWARD_STRESS22
        )
        assert len(rows_a) != len(rows_b)


class TestCanonicalOrderingAndHash:
    def test_hash_independent_of_input_order(self):
        t1 = _s3_trade(signal_ts=0, symbol="XRPUSDT")
        t2 = _s3_trade(signal_ts=60_000, symbol="DOGEUSDT")
        rows_ab = build_s3_scenario_ledger([t1, t2], PATH_SCENARIO_BASE13)
        rows_ba = build_s3_scenario_ledger([t2, t1], PATH_SCENARIO_BASE13)
        assert s3_ledger_hash(rows_ab) == s3_ledger_hash(rows_ba)

    def test_hash_changes_on_one_ulp_gross_bps_mutation(self):
        import math

        t1 = _s3_trade(gross_bps=200.0)
        t2 = _s3_trade(gross_bps=math.nextafter(200.0, math.inf))
        rows1 = build_s3_scenario_ledger([t1], PATH_SCENARIO_BASE13)
        rows2 = build_s3_scenario_ledger([t2], PATH_SCENARIO_BASE13)
        assert s3_ledger_hash(rows1) != s3_ledger_hash(rows2)

    def test_s4_hash_independent_of_input_order(self):
        t1 = _s4_trade(signal_ts=0)
        t2 = _s4_trade(signal_ts=60_000, pair=("DOGEUSDT", "SOLUSDT"))
        rows_ab = build_s4_scenario_ledger([t1, t2], PATH_SCENARIO_BASE13)
        rows_ba = build_s4_scenario_ledger([t2, t1], PATH_SCENARIO_BASE13)
        assert s4_ledger_hash(rows_ab) == s4_ledger_hash(rows_ba)


class TestExceptionPropagation:
    def test_funding_lookup_exception_propagates_unwrapped(self):
        trade = _s3_trade()

        def broken_lookup(symbol, side, entry_ts, exit_ts):
            raise KeyError("deliberately broken lookup")

        with pytest.raises(KeyError):
            build_s3_scenario_ledger([trade], PATH_SCENARIO_BASE13, broken_lookup)
