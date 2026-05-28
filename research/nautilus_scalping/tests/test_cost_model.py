"""ROB-351 (Issue 4) — shared net-at-fee primitive + 3->1 dedup regression.

The linear fee rescale ``net(fee) = net_ref + commission_ref*(1 - fee/ref)`` was
duplicated in validated_gate / fee_sweep / compare_strategies. These tests pin
the extracted primitive's arithmetic AND prove validated_gate's call-site is
behavior-identical after delegating to it (REGRESSION).
"""

import cost_model
import validated_gate
from validated_gate import Trade


def test_net_at_fee_endpoints():
    # as-run at ref fee: scale=0 -> just net_ref_pnl
    assert cost_model.net_at_fee(5.0, 2.0, fee_bps=10.0, ref_fee_bps=10.0) == 5.0
    # zero fee: scale=1 -> commission fully added back (gross)
    assert cost_model.net_at_fee(5.0, 2.0, fee_bps=0.0, ref_fee_bps=10.0) == 7.0
    # half fee: scale=0.5
    assert cost_model.net_at_fee(5.0, 2.0, fee_bps=5.0, ref_fee_bps=10.0) == 6.0


def test_net_at_fee_default_ref_is_ten():
    assert cost_model.REF_FEE_BPS == 10.0
    assert cost_model.net_at_fee(0.0, 4.0, fee_bps=0.0) == 4.0


def test_regression_validated_gate_uses_primitive():
    # Golden trades; commission_ref positive (maker_fill convention).
    trades = [
        Trade(net_ref_pnl=3.0, commission_ref=1.0, notional=100.0, ts_opened=1),
        Trade(net_ref_pnl=-2.0, commission_ref=2.0, notional=100.0, ts_opened=2),
        Trade(net_ref_pnl=0.5, commission_ref=0.4, notional=50.0, ts_opened=3),
    ]
    for fee in (0.0, 2.0, 5.0, 7.5, 10.0):
        for t in trades:
            assert validated_gate._net_at_fee(t, fee) == cost_model.net_at_fee(
                t.net_ref_pnl, t.commission_ref, fee
            )
