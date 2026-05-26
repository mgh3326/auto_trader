# research/nautilus_scalping/tests/test_maker_fill.py
"""ROB-324 — pure maker/limit-fill scenario builders.

Records in, validated_gate.Trade lists out. No nautilus; fully deterministic."""
from __future__ import annotations

from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    MakerTradeRecord,
    build_maker_optimistic,
    build_maker_conservative,
    classify_easy_tp,
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


def test_classify_easy_tp_boundary() -> None:
    # TP hit with tiny adverse excursion = easy (front-of-queue) fill
    assert classify_easy_tp(_rec(0.5, 0.04, 100, 1, tp_hit=True, adverse=1.0)) is True
    # TP hit but price moved against us first = a real fill, not easy
    assert classify_easy_tp(_rec(0.5, 0.04, 100, 1, tp_hit=True, adverse=5.0)) is False
    # SL exits are never "easy TP"
    assert classify_easy_tp(_rec(-0.5, 0.04, 100, 1, tp_hit=False, adverse=0.0)) is False


def test_conservative_applies_adverse_cost_to_survivors() -> None:
    # a non-easy filled trade is kept but charged the adverse cost
    recs = [_rec(0.50, 0.04, 100.0, 1, tp_hit=True, adverse=9.0)]  # not easy -> kept
    trades = build_maker_conservative(recs, queue_loss_pct=0.25, adverse_bps=1.0)
    assert len(trades) == 1
    # adverse cost = 1.0 bp * 100 notional / 10_000 = 0.01
    assert round(trades[0].net_ref_pnl, 4) == 0.49


def test_conservative_drops_about_quarter_of_easy_tp_fills_deterministically() -> None:
    easy = [_rec(0.50, 0.04, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(2000)]
    first = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    second = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    # deterministic across runs (hash of ts, not RNG)
    assert [t.ts_opened for t in first] == [t.ts_opened for t in second]
    kept = len(first)
    dropped = 2000 - kept
    assert 400 <= dropped <= 600          # ~25% dropped (hash uniformity tolerance)


def test_conservative_excludes_missed_fills() -> None:
    recs = [_rec(0.0, 0.0, 100.0, 1, filled=False)]
    assert build_maker_conservative(recs) == []


def test_conservative_is_strictly_worse_than_optimistic() -> None:
    recs = [_rec(0.50, 0.04, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(500)]
    from validated_gate import metrics_at_fee
    opt = metrics_at_fee(build_maker_optimistic(recs), 10.0).net_pnl
    con = metrics_at_fee(build_maker_conservative(recs), 10.0).net_pnl
    assert con < opt                      # queue-loss + adverse cost only ever hurt
