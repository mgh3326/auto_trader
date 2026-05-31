"""ROB-384 — failure-mode taxonomy tests (deterministic per known candidate)."""

from __future__ import annotations

from external_strategy_sieve.postmortem import taxonomy
from external_strategy_sieve.postmortem.evidence import CandidateEvidence


def _ev(**kw):
    base = {
        "issue": "X",
        "candidate": "c",
        "family": "f",
        "source": "reparsed",
        "schema": "s",
        "citation": "cit",
    }
    base.update(kw)
    return CandidateEvidence(**base)


def _modes(**kw):
    return set(taxonomy.assign_failure_modes(_ev(**kw))[0])


def test_gross_negative_is_gross_zero():
    # range_filter / ROB-353 families: gross <= 0.
    m = _modes(gross_bps=-5.52, net_bps_by_fee={"4": -13.5, "10": -25.5})
    assert "gross_zero" in m
    assert "cost_dominated" not in m


def test_gross_negative_small_sample_is_not_underpowered():
    # ROB-353 family2: gross -27.53, only 58 periods. No gross edge -> sample size
    # is moot; must NOT be tagged single_fold_only.
    m = _modes(
        gross_bps=-27.53, net_bps_by_fee={}, trade_count=58, verdict="screened_out"
    )
    assert m == {"gross_zero"}


def test_meanrev_is_cost_dominated_not_fee_fragile():
    # gross +0.16, net@4 -0.97 (already negative at demo taker) -> cost_dominated only.
    m = _modes(
        gross_bps=0.1584,
        net_bps_by_fee={"4": -0.97, "10": -2.66},
        single_fold_edge=False,
    )
    assert "cost_dominated" in m
    assert "fee_fragile" not in m
    assert "gross_zero" not in m


def test_bbrsi_shadow_is_cost_dominated_fee_fragile_and_shadow():
    m = _modes(
        gross_bps=10.32,
        net_bps_by_fee={"4": 2.32, "10": -9.68},
        single_fold_edge=False,
        verdict="validated / sieve_class=shadow_candidate",
    )
    assert {"cost_dominated", "fee_fragile", "license_shadow_only"} <= m


def test_squeeze_is_single_fold_and_source_unfaithful():
    m = _modes(
        gross_bps=13.60,
        net_bps_by_fee={"4": 5.60, "10": -6.40},
        single_fold_edge=True,
        verdict="not_validated / sieve_class=research_candidate",
        notes="caveat: non_faithful_clean_room_spec: momentum simplified",
    )
    assert "single_fold_only" in m
    assert "source_unfaithful" in m
    assert "fee_fragile" in m  # net@4>0, net@10<0


def test_supertrend_cost_dominated_and_single_fold():
    m = _modes(
        gross_bps=4.37, net_bps_by_fee={"4": -3.63, "10": -15.63}, single_fold_edge=True
    )
    assert {"cost_dominated", "single_fold_only"} <= m


def test_ichi_cost_dominated_and_fee_fragile_not_single_fold():
    # adequate sample (830), gate validated -> not underpowered; net@retail negative.
    m = _modes(
        gross_bps=15.26,
        net_bps_by_fee={"4": 7.26, "10": -4.74},
        trade_count=830,
        oos_trade_count=385,
        t_stat_oos=1.185,
        verdict="gross_edge_present_AND_oos_validated / gate=validated / decisive_survivor=False",
    )
    assert {"cost_dominated", "fee_fragile"} <= m
    assert "single_fold_only" not in m  # 830 trades, not underpowered


def test_elliot_underpowered_is_single_fold_only():
    # gross +129, net@retail STILL positive (+109) -> not cost/fee; tiny n=18.
    m = _modes(
        gross_bps=129.73,
        net_bps_by_fee={"4": 121.73, "10": 109.73},
        trade_count=18,
        verdict="gross_edge_present_but_underpowered / gate=insufficient_data",
    )
    assert "single_fold_only" in m
    assert (
        "cost_dominated" not in m and "gross_zero" not in m and "fee_fragile" not in m
    )


def test_documented_rob342_cost_dominated_no_net_grid():
    # gross +0.44, no net grid published -> tiny gross below demo hurdle.
    m = _modes(
        gross_bps=0.44, net_bps_by_fee={}, source="documented", schema="documented"
    )
    assert "cost_dominated" in m


def test_every_candidate_gets_at_least_one_mode():
    # A pathological gross-positive, net-positive, well-powered, no-tstat row.
    modes, _ = taxonomy.assign_failure_modes(
        _ev(gross_bps=5.0, net_bps_by_fee={"4": 4.0, "10": 3.0}, trade_count=500)
    )
    assert len(modes) >= 1


def test_annotate_sets_modes_in_place():
    recs = [_ev(gross_bps=-1.0)]
    taxonomy.annotate(recs)
    assert recs[0].failure_modes == ["gross_zero"]
    assert "taxonomy:" in recs[0].notes
