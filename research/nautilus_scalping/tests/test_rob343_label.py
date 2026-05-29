"""ROB-351 (eng-review Issue 3 + Codex realistic-path) — the 343 handoff label.

Closability is decided in the PURE path: the maker-conservative scenario
(maker_fill queue-loss + adverse-selection haircuts) must itself be net-positive
for the candidate to be a ROB-343 hand-off. Cost-binding alone is NOT enough
(Codex realistic-path stop-rule). Taker breakeven is reported as evidence only.
"""

import math

import rob343_label as r
from validated_gate import Trade


def test_breakeven_taker_fee_closed_form():
    # sum_net_ref=-5, sum_comm=10 -> net(fee)=0 at fee=5 bps/leg
    trades = [Trade(net_ref_pnl=-5.0, commission_ref=10.0, notional=100.0, ts_opened=1)]
    assert math.isclose(r.breakeven_taker_fee_bps(trades), 5.0)


def test_breakeven_no_commission_dependence():
    # zero commission -> fee cannot change net; positive net -> +inf headroom
    trades = [Trade(net_ref_pnl=3.0, commission_ref=0.0, notional=100.0, ts_opened=1)]
    assert r.breakeven_taker_fee_bps(trades) == math.inf


def test_already_net_viable_is_promote_to_pilot():
    v = r.label_343_candidate(taker_net_pnl=4.0, gross_pnl=6.0,
                              maker_conservative_net=5.0, oos_significant=True,
                              breakeven_taker_bps=8.0)
    assert v.label == "promote_to_pilot"
    assert v.cost_binding is False


def test_cost_binding_and_closable_is_343_candidate():
    v = r.label_343_candidate(taker_net_pnl=-2.0, gross_pnl=6.0,
                              maker_conservative_net=1.5, oos_significant=True,
                              breakeven_taker_bps=3.0)
    assert v.label == "cost_binding_343_candidate"
    assert v.cost_binding is True
    assert v.closable is True


def test_cost_binding_but_not_closable_is_reject():
    # gross positive, killed by taker fees, but maker-conservative STILL negative
    v = r.label_343_candidate(taker_net_pnl=-2.0, gross_pnl=6.0,
                              maker_conservative_net=-0.5, oos_significant=True,
                              breakeven_taker_bps=3.0)
    assert v.label == "reject"
    assert v.cost_binding is True
    assert v.closable is False


def test_no_gross_edge_is_reject():
    v = r.label_343_candidate(taker_net_pnl=-3.0, gross_pnl=-1.0,
                              maker_conservative_net=-2.0, oos_significant=True,
                              breakeven_taker_bps=0.0)
    assert v.label == "reject"
    assert v.cost_binding is False


def test_not_significant_is_needs_more_data():
    v = r.label_343_candidate(taker_net_pnl=-2.0, gross_pnl=6.0,
                              maker_conservative_net=1.5, oos_significant=False,
                              breakeven_taker_bps=3.0)
    assert v.label == "needs_more_data"
