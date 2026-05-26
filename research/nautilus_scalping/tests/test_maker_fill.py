# research/nautilus_scalping/tests/test_maker_fill.py
"""ROB-324 — pure maker/limit-fill scenario builders.

Records in, validated_gate.Trade lists out. No nautilus; fully deterministic."""
from __future__ import annotations

from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    MakerTradeRecord,
    build_maker_optimistic,
)
from validated_gate import Trade, metrics_at_fee


def test_module_constants_match_demo_fees() -> None:
    assert MAKER_FEE_BPS == 2.0
    assert TAKER_BASELINE_BPS == 4.0


def _rec(net, comm, notional, ts, *, filled=True, tp_hit=True, adverse=0.0) -> MakerTradeRecord:
    return MakerTradeRecord(
        net_at_real_fees=net, commission_real=comm, notional=notional,
        ts_opened=ts, filled=filled, tp_hit=tp_hit, adverse_excursion_bps=adverse,
    )


def test_record_is_frozen_dataclass() -> None:
    r = _rec(1.0, 0.04, 100.0, 0)
    assert r.net_at_real_fees == 1.0 and r.filled is True


def test_optimistic_excludes_missed_fills_and_preserves_true_net() -> None:
    recs = [
        _rec(0.50, 0.04, 100.0, 1, filled=True),
        _rec(0.00, 0.00, 100.0, 2, filled=False),   # missed fill -> dropped
        _rec(-0.30, 0.04, 100.0, 3, filled=True),
    ]
    trades = build_maker_optimistic(recs)
    assert len(trades) == 2                          # missed fill excluded
    assert all(isinstance(t, Trade) for t in trades)
    # net_after_cost at the gate's reference point == as-run true net
    m = metrics_at_fee(trades, fee_bps=10.0, fold="net_after_cost")
    assert round(m.net_pnl, 2) == 0.20               # 0.50 + (-0.30)
    # gross (fee=0) adds the real commission back
    g = metrics_at_fee(trades, fee_bps=0.0, fold="gross")
    assert round(g.net_pnl, 2) == 0.28               # 0.20 + 0.04 + 0.04
