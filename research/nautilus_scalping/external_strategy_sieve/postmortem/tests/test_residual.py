"""ROB-384 — residual map + closure decision tests."""

from __future__ import annotations

from external_strategy_sieve.postmortem import residual
from external_strategy_sieve.postmortem.evidence import CandidateEvidence


def _ev(modes, **kw):
    base = {
        "issue": "X",
        "candidate": "c",
        "family": "f",
        "source": "reparsed",
        "schema": "s",
        "citation": "cit",
    }
    base.update(kw)
    ev = CandidateEvidence(**base)
    ev.failure_modes = modes
    return ev


def test_gross_zero_is_closed():
    s, _ = residual.residual_status(_ev(["gross_zero"], gross_bps=-5.0))
    assert s == "closed"


def test_meanrev_cost_dominated_maker_fill_tested_is_closed():
    ev = _ev(
        ["cost_dominated"],
        issue="ROB-320",
        candidate="meanrev_zscore_fade",
        gross_bps=0.16,
        net_bps_by_fee={"4": -0.97, "10": -2.66},
        trade_count=789,
    )
    s, reason = residual.residual_status(ev)
    assert s == "closed"
    assert "ROB-324" in reason


def test_untested_stable_cost_dominated_is_feasibility():
    # A hypothetical stable, fee-only-killed edge whose maker-fill is untested.
    ev = _ev(
        ["cost_dominated"],
        issue="ROB-999",
        candidate="hypothetical_stable",
        gross_bps=8.0,
        net_bps_by_fee={"4": 3.0, "10": -1.0},
        trade_count=1000,
        t_stat_oos=2.5,
    )
    s, _ = residual.residual_status(ev)
    assert s == "maybe_worth_feasibility"


def test_ichi_fee_fragile_low_toos_is_not_worth_pursuing():
    ev = _ev(
        ["cost_dominated", "fee_fragile"],
        issue="ROB-382",
        candidate="ichi",
        gross_bps=15.26,
        net_bps_by_fee={"4": 7.26, "10": -4.74},
        trade_count=830,
        t_stat_oos=1.185,
        verdict="... / decisive_survivor=False",
    )
    s, reason = residual.residual_status(ev)
    assert s == "not_worth_pursuing"
    assert "1.18" in reason or "decisive" in reason


def test_squeeze_single_fold_is_not_worth_pursuing():
    ev = _ev(
        ["cost_dominated", "fee_fragile", "single_fold_only", "source_unfaithful"],
        single_fold_edge=True,
        gross_bps=13.6,
        net_bps_by_fee={"4": 5.6, "10": -6.4},
    )
    s, _ = residual.residual_status(ev)
    assert s == "not_worth_pursuing"


def _full_known_set():
    """The real ROB-384 candidate set, post-taxonomy, as residual inputs."""
    return [
        _ev(
            ["cost_dominated"],
            issue="ROB-320",
            candidate="meanrev_zscore_fade",
            gross_bps=0.16,
            net_bps_by_fee={"4": -0.97, "10": -2.66},
            trade_count=789,
        ),
        _ev(
            ["cost_dominated", "single_fold_only"],
            issue="ROB-383",
            candidate="freqtrade_supertrend",
            single_fold_edge=True,
            gross_bps=4.37,
            net_bps_by_fee={"4": -3.63, "10": -15.63},
        ),
        _ev(
            ["cost_dominated", "fee_fragile", "license_shadow_only"],
            issue="ROB-383",
            candidate="freqtrade_bbrsi_naive",
            gross_bps=10.32,
            net_bps_by_fee={"4": 2.32, "10": -9.68},
            verdict="validated / sieve_class=shadow",
        ),
        _ev(
            ["cost_dominated", "fee_fragile", "single_fold_only", "source_unfaithful"],
            issue="ROB-383",
            candidate="tv_squeeze_momentum",
            single_fold_edge=True,
            gross_bps=13.6,
            net_bps_by_fee={"4": 5.6, "10": -6.4},
        ),
        _ev(
            ["gross_zero"],
            issue="ROB-383",
            candidate="tv_range_filter",
            gross_bps=-5.52,
        ),
        _ev(
            ["cost_dominated", "fee_fragile", "license_shadow_only"],
            issue="ROB-383",
            candidate="tv_chandelier_exit",
            gross_bps=10.52,
            net_bps_by_fee={"4": 2.52, "10": -9.48},
            verdict="validated / sieve_class=shadow",
        ),
        _ev(
            ["cost_dominated", "fee_fragile"],
            issue="ROB-382",
            candidate="ichi",
            gross_bps=15.26,
            net_bps_by_fee={"4": 7.26, "10": -4.74},
            trade_count=830,
            t_stat_oos=1.185,
            baseline_beat={"micro_breakout": True},
            verdict="... / decisive_survivor=False",
        ),
        _ev(
            ["single_fold_only"],
            issue="ROB-382",
            candidate="elliot",
            gross_bps=129.73,
            net_bps_by_fee={"4": 121.73, "10": 109.73},
            trade_count=18,
            t_stat_oos=1.6,
            baseline_beat={"micro_breakout": True},
            verdict="underpowered / gate=insufficient_data",
        ),
        _ev(
            ["single_fold_only"],
            issue="ROB-382",
            candidate="vwap",
            gross_bps=69.17,
            net_bps_by_fee={"4": 61.17, "10": 49.17},
            trade_count=43,
            t_stat_oos=1.0,
            baseline_beat={"micro_breakout": True},
            verdict="underpowered / gate=insufficient_data",
        ),
        _ev(
            ["single_fold_only"],
            issue="ROB-382",
            candidate="cluc",
            gross_bps=129.85,
            net_bps_by_fee={"4": 121.85, "10": 109.85},
            trade_count=198,
            t_stat_oos=1.684,
            baseline_beat={"micro_breakout": True},
            verdict="underpowered / gate=insufficient_data",
        ),
        _ev(
            ["gross_zero"],
            issue="ROB-353",
            candidate="family1_breakout_continuation",
            gross_bps=-70.99,
        ),
        _ev(
            ["gross_zero"],
            issue="ROB-353",
            candidate="family2_ts_trend_basket",
            gross_bps=-27.53,
        ),
        _ev(
            ["gross_zero"],
            issue="ROB-353",
            candidate="family3_xs_momentum",
            gross_bps=-39.38,
        ),
        _ev(
            ["cost_dominated"],
            issue="ROB-342",
            candidate="short_horizon_sweep_reversal",
            source="documented",
            schema="documented",
            gross_bps=0.44,
            net_bps_by_fee={},
        ),
    ]


def test_full_set_closure_is_A():
    decision = residual.closure_decision(_full_known_set())
    assert decision["verdict"] == "A"
    assert decision["n_maybe_worth_feasibility"] == 0
    assert decision["c_candidates"] == []
    # every candidate resolves to closed or not_worth_pursuing
    assert set(decision["status_distribution"]) <= {"closed", "not_worth_pursuing"}
    assert decision["n_candidates"] == 14


def test_one_untested_feasibility_flips_to_B():
    recs = _full_known_set()
    recs.append(
        _ev(
            ["cost_dominated"],
            issue="ROB-777",
            candidate="untested_stable",
            gross_bps=9.0,
            net_bps_by_fee={"4": 4.0, "10": -1.0},
            trade_count=2000,
            t_stat_oos=3.0,
        )
    )
    assert residual.closure_decision(recs)["verdict"] == "B"


def test_one_open_underpowered_survivor_flips_to_C():
    recs = _full_known_set()
    # gross-positive, beats baseline, t_oos<2, but source did NOT close it.
    recs.append(
        _ev(
            ["single_fold_only"],
            issue="ROB-888",
            candidate="open_underpowered",
            gross_bps=20.0,
            net_bps_by_fee={"4": 12.0, "10": 4.0},
            trade_count=60,
            t_stat_oos=1.5,
            baseline_beat={"micro_breakout": True},
            verdict="open_for_revalidation",
        )
    )
    assert residual.closure_decision(recs)["verdict"] == "C"
