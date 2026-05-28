"""ROB-351 (eng-review Issue 5) — cost-blind screen mode + cost_binding label.

Single classifier (no parallel 343 classifier): the cost-blind mode screens on
GROSS sign (fees=0) with an economic-triviality floor, and flags
``cost_binding`` when a candidate is positive-gross but cost-killed (the cheap
screen-level 343 signal; deeper closability lives in the maker_fill path).
"""

from discovery.screen import HypothesisSummary, classify


def _summary(gross, fee_adj, *, oos=None, oos_gross=None, samples=300, name="h"):
    return HypothesisSummary(
        name=name, conditions="c", sample_count=samples,
        gross_expectancy_bps=gross, fee_adjusted_bps=fee_adj, oos_fee_adjusted_bps=oos,
        oos_gross_bps=oos_gross,
    )


def test_default_mode_unchanged_and_cost_binding_false():
    c = classify(_summary(5.0, 3.0))
    assert c.recommendation == "promote_to_full_validation"
    assert c.cost_binding is False


def test_cost_blind_positive_gross_but_cost_killed_is_cost_binding():
    c = classify(_summary(5.0, -2.0), cost_blind=True)
    assert c.recommendation == "promote_to_full_validation"
    assert c.in_sample_only is True
    assert c.cost_binding is True  # positive gross, killed by fees -> a 343 signal


def test_cost_blind_already_net_viable_is_not_cost_binding():
    c = classify(_summary(5.0, 3.0), cost_blind=True)
    assert c.recommendation == "promote_to_full_validation"
    assert c.cost_binding is False  # survives net too -> not cost-binding


def test_cost_blind_negative_gross_screened_out():
    c = classify(_summary(-1.0, -3.0), cost_blind=True)
    assert c.recommendation == "screened_out"
    assert c.cost_binding is False


def test_cost_blind_economic_triviality_floor():
    # gross positive but below the floor -> screened_out (Codex: sign>0 too low)
    c = classify(_summary(0.3, -0.1), cost_blind=True, min_gross_bps=0.5)
    assert c.recommendation == "screened_out"
    assert "triviality" in c.reason or "floor" in c.reason


def test_cost_blind_low_samples_needs_more_data():
    c = classify(_summary(5.0, -2.0, samples=10), cost_blind=True, min_samples=200)
    assert c.recommendation == "needs_more_data"


def test_cost_blind_oos_gross_sign_must_agree():
    # in-sample gross positive but OOS GROSS negative -> screened_out
    c = classify(_summary(5.0, -2.0, oos_gross=-1.0), cost_blind=True)
    assert c.recommendation == "screened_out"
