# tests/test_maker_fill.py
"""ROB-324 — pure maker/limit-fill scenario builders.

Records (zero-fee gross + notionals) in, validated_gate.Trade lists out. No
nautilus; fully deterministic. Fees applied per leg: maker 2bps on entry + the TP
leg, taker 4bps on the SL leg."""
from __future__ import annotations

from maker_fill import (
    MAKER_FEE_BPS,
    TAKER_BASELINE_BPS,
    MakerTradeRecord,
    build_maker_conservative,
    build_maker_optimistic,
    classify_easy_tp,
)
from validated_gate import Trade, evaluate_gate, metrics_at_fee


def _rec(gross, notional, ts, *, filled=True, tp_hit=True, adverse=0.0) -> MakerTradeRecord:
    return MakerTradeRecord(
        gross=gross, entry_notional=notional, exit_notional=notional,
        ts_opened=ts, filled=filled, tp_hit=tp_hit, adverse_excursion_bps=adverse,
    )


def test_module_constants_match_demo_fees() -> None:
    assert MAKER_FEE_BPS == 2.0
    assert TAKER_BASELINE_BPS == 4.0


def test_record_is_frozen_dataclass() -> None:
    r = _rec(1.0, 100.0, 0)
    assert r.gross == 1.0 and r.filled is True and r.entry_notional == 100.0


def test_optimistic_excludes_missed_fills_and_applies_maker_fees() -> None:
    # tp_hit => exit leg is maker (2bps); fee = 2bps*100/1e4 (entry) + 2bps*100/1e4 (TP) = 0.04
    recs = [
        _rec(0.50, 100.0, 1, filled=True, tp_hit=True),
        _rec(0.00, 100.0, 2, filled=False),              # missed fill -> dropped
        _rec(-0.30, 100.0, 3, filled=True, tp_hit=True),
    ]
    trades = build_maker_optimistic(recs)
    assert len(trades) == 2                              # missed fill excluded
    assert all(isinstance(t, Trade) for t in trades)
    # net_after_cost at the gate reference point == as-run true net (gross - fees)
    m = metrics_at_fee(trades, fee_bps=10.0, fold="net_after_cost")
    assert round(m.net_pnl, 4) == round((0.50 - 0.04) + (-0.30 - 0.04), 4)   # 0.12
    # gross column (fee=0) adds the fee back -> recovers the fee-free gross sum
    g = metrics_at_fee(trades, fee_bps=0.0, fold="gross")
    assert round(g.net_pnl, 4) == round(0.50 + (-0.30), 4)                   # 0.20


def test_sl_exit_charges_taker_fee_on_exit_leg() -> None:
    # SL exit (tp_hit=False): fee = 2bps entry + 4bps exit = (0.02 + 0.04) on 100 notional = 0.06
    t = build_maker_optimistic([_rec(-0.40, 100.0, 1, tp_hit=False)])[0]
    assert round(t.commission_ref, 4) == 0.06
    assert round(t.net_ref_pnl, 4) == round(-0.40 - 0.06, 4)


def test_classify_easy_tp_boundary() -> None:
    assert classify_easy_tp(_rec(0.5, 100, 1, tp_hit=True, adverse=1.0)) is True
    assert classify_easy_tp(_rec(0.5, 100, 1, tp_hit=True, adverse=5.0)) is False
    assert classify_easy_tp(_rec(-0.5, 100, 1, tp_hit=False, adverse=0.0)) is False


def test_conservative_applies_adverse_cost_to_survivors() -> None:
    # not-easy TP fill (adverse 9 > eps) -> kept, charged fees + adverse cost
    recs = [_rec(0.50, 100.0, 1, tp_hit=True, adverse=9.0)]
    t = build_maker_conservative(recs, queue_loss_pct=0.25, adverse_bps=1.0)[0]
    # fee 0.04 (maker entry + maker TP) + adverse 1bps*100/1e4 = 0.01
    assert round(t.net_ref_pnl, 4) == round(0.50 - 0.04 - 0.01, 4)           # 0.45


def test_conservative_drops_about_quarter_of_easy_tp_fills_deterministically() -> None:
    easy = [_rec(0.50, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(2000)]
    first = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    second = build_maker_conservative(easy, queue_loss_pct=0.25, adverse_bps=1.0)
    assert [t.ts_opened for t in first] == [t.ts_opened for t in second]     # deterministic
    dropped = 2000 - len(first)
    assert 400 <= dropped <= 600                                            # ~25%


def test_conservative_excludes_missed_fills() -> None:
    assert build_maker_conservative([_rec(0.0, 100.0, 1, filled=False)]) == []


def test_conservative_is_strictly_worse_than_optimistic() -> None:
    recs = [_rec(0.50, 100.0, ts, tp_hit=True, adverse=0.0) for ts in range(500)]
    opt = metrics_at_fee(build_maker_optimistic(recs), 10.0).net_pnl
    con = metrics_at_fee(build_maker_conservative(recs), 10.0).net_pnl
    assert con < opt                                                        # haircuts only hurt


def test_maker_scenario_verdict_stays_in_vocabulary() -> None:
    recs = [_rec(0.20, 100.0, ts, tp_hit=(ts % 2 == 0), adverse=float(ts % 5))
            for ts in range(400)]
    losers = [Trade(net_ref_pnl=-0.5, commission_ref=0.04, notional=100.0, ts_opened=ts)
              for ts in range(400)]
    for builder in (build_maker_optimistic, build_maker_conservative):
        trades = builder(recs)
        report = evaluate_gate(
            candidate_runs={"maker_fill": trades},
            baseline_breakout=losers, baseline_random=losers,
            fee_bps=10.0, min_trades=100,
            candidate_name="t", hypothesis="mean_reversion", symbols=["XRPUSDT"])
        assert report.verdict in {"validated", "not_validated", "insufficient_data"}
