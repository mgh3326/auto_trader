"""ROB-943 (H3, ROB-940) — S1 Donchian-15m signal generator RED/GREEN tests."""

from __future__ import annotations

import dataclasses

import pytest
from rob940_bars_agg import AggregatedBar
from rob940_signal_manifest import S1Config, get_s1_config
from rob940_signal_s1 import _rolling_median, _wilder_atr_series, generate_s1_signals

_BUCKET_MS = 15 * 60_000


def _bar(idx: int, o, h, low, c, v, *, segment_start: bool = False) -> AggregatedBar:
    ts = idx * _BUCKET_MS
    return AggregatedBar(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        close_ts=ts + _BUCKET_MS,
        is_segment_start=segment_start,
    )


def _flat_warmup(
    n: int, *, h=100.15, low=99.85, c=100.0, v=100.0
) -> list[AggregatedBar]:
    bars = []
    for i in range(n):
        bars.append(_bar(i, c, h, low, c, v, segment_start=(i == 0)))
    return bars


# ---------------------------------------------------------------------------
# Pure helper unit tests (hand-verified tiny fixtures)
# ---------------------------------------------------------------------------


def test_wilder_atr_seed_is_simple_average_of_first_period_trs():
    # period=3: TR_1..TR_3 constant at 2.0 (flat H/L/C=100+-1), seed=2.0.
    bars = [
        _bar(0, 100, 101, 99, 100, 10, segment_start=True),
        _bar(1, 100, 101, 99, 100, 10),
        _bar(2, 100, 101, 99, 100, 10),
        _bar(3, 100, 101, 99, 100, 10),
    ]
    atr = _wilder_atr_series(bars, period=3)
    assert atr[0] is None
    assert atr[1] is None
    assert atr[2] is None
    assert atr[3] == 2.0  # seed = avg(TR_1,TR_2,TR_3) = avg(2,2,2)


def test_wilder_atr_recurrence_after_seed():
    # period=2: TR_1=TR_2=2.0 -> seed ATR_2=2.0. TR_3: H=104,L=100,Cprev=100
    # -> TR=max(4,4,0)=4.0 -> ATR_3=(2.0*1+4.0)/2=3.0.
    bars = [
        _bar(0, 100, 101, 99, 100, 10, segment_start=True),
        _bar(1, 100, 101, 99, 100, 10),
        _bar(2, 100, 101, 99, 100, 10),
        _bar(3, 100, 104, 100, 104, 10),
    ]
    atr = _wilder_atr_series(bars, period=2)
    assert atr[2] == 2.0
    assert atr[3] == 3.0


def test_segment_slices_splits_on_gap():
    from rob940_signal_s1 import _segment_slices

    bars = [
        _bar(0, 100, 101, 99, 100, 10, segment_start=True),
        _bar(1, 100, 101, 99, 100, 10),
        _bar(2, 100, 999, 1, 500, 10, segment_start=True),  # new segment
        _bar(3, 500, 501, 499, 500, 10),
    ]
    assert _segment_slices(bars) == [(0, 2), (2, 4)]


def test_generator_resets_warmup_after_gap_no_cross_segment_leakage():
    cfg = get_s1_config("S1-00")  # L=16, needs 20 bars warm-up (dominant)
    warm = _flat_warmup(20)  # a full, otherwise-valid warm-up segment
    # A brand-new segment starts right at what would be the breakout bar:
    # only 1 bar exists in THIS segment, so warm-up must NOT be considered
    # satisfied even though 20 bars preceded it in the (gapped-off) old one.
    post_gap_breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.1,
        high=100.30,
        low=100.0,
        close=100.30,
        volume=125.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=True,  # <-- gap boundary
    )
    signals = generate_s1_signals([*warm, post_gap_breakout], cfg, symbol="XRPUSDT")
    assert signals == ()


def test_rolling_median_excludes_current_bar():
    values = [10.0, 10.0, 10.0, 999.0]  # current bar's own huge volume excluded
    med = _rolling_median(values, window=3, idx=3)
    assert med == 10.0


def test_rolling_median_none_before_window_full():
    values = [10.0, 10.0]
    assert _rolling_median(values, window=3, idx=1) is None


# ---------------------------------------------------------------------------
# Generator-level tests
# ---------------------------------------------------------------------------


def test_no_signal_before_warmup_complete():
    cfg = get_s1_config("S1-00")  # L=16
    bars = _flat_warmup(20)  # index 0..19, ATR/volume need index>=20
    signals = generate_s1_signals(bars, cfg, symbol="XRPUSDT")
    assert signals == ()


def test_long_breakout_chase_boundary_exactly_half_atr_passes():
    cfg = get_s1_config("S1-00")  # L=16, q_min=1.25, k_SL=1.25, R_TP=1.80
    bars = _flat_warmup(20)  # indices 0..19, flat H=100.15/L=99.85/C=100 -> TR=0.3
    # U (window [4:20)) = 100.15. TR_20 chosen = 0.3 too (H=C=100.30, L=100.0)
    # so ATR stays clean at 0.3: seed=(19*0.3+0.3)/20=0.3.
    # chase=(C-U)/ATR=(100.30-100.15)/0.3=0.5 exactly (upper boundary, inclusive).
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.1,
        high=100.30,  # =close, no upper wick
        low=100.0,
        close=100.30,
        volume=125.0,  # q = 125/100 = 1.25 exactly (boundary pass)
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    bars = [*bars, breakout]
    signals = generate_s1_signals(bars, cfg, symbol="XRPUSDT")
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side == "long"
    assert sig.signal_ts == breakout.close_ts
    assert sig.strategy == "S1"
    assert sig.config_id == "S1-00"
    assert sig.symbol == "XRPUSDT"
    assert sig.timeout_bars == 180
    assert sig.cooldown_bars == 60
    # k_SL*a_t = 1.25*0.3/100.30 = 0.3738% < 0.45% floor -> clipped.
    assert round(sig.sl_distance_bps, 6) == 45.0
    assert round(sig.tp_distance_bps, 6) == 81.0  # R_TP=1.80 * 45bp


def test_strict_breakout_equal_to_upper_band_does_not_trigger():
    cfg = get_s1_config("S1-00")
    bars = _flat_warmup(20)
    at_band = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.15,
        high=100.15,
        low=99.85,
        close=100.15,  # exactly == U, not strictly above
        volume=200.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, at_band], cfg, symbol="XRPUSDT")
    assert signals == ()


def test_short_breakout_symmetric_to_long():
    cfg = get_s1_config("S1-00")  # D (window [4:20)) = 99.85
    bars = _flat_warmup(20)
    # Mirror of the long chase=0.5-boundary fixture: TR_20 kept at 0.3 (clean
    # ATR=0.3) via H=100.0(=Cprev)/L=C=99.70, so chase=(D-C)/ATR=0.5 exactly.
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=99.9,
        high=100.0,
        low=99.70,  # =close, no lower wick
        close=99.70,
        volume=125.0,  # q = 1.25 exactly
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side == "short"
    assert round(sig.sl_distance_bps, 6) == 45.0
    assert round(sig.tp_distance_bps, 6) == 81.0


def test_volume_gate_below_q_min_produces_no_signal():
    cfg = get_s1_config("S1-00")  # q_min=1.25
    bars = _flat_warmup(20)
    # Same shape as the chase=0.5-boundary fixture (breakout/chase/a_t all
    # otherwise pass) except volume is 1 unit short of the q_min threshold.
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.1,
        high=100.30,
        low=100.0,
        close=100.30,
        volume=124.0,  # q = 1.24 < 1.25
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert signals == ()


def test_chase_beyond_half_atr_produces_no_signal():
    cfg = get_s1_config("S1-00")
    bars = _flat_warmup(20)
    far_breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.1,
        high=101.6,
        low=100.6,
        close=101.6,  # far above U, chase ratio > 0.5
        volume=125.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, far_breakout], cfg, symbol="XRPUSDT")
    assert signals == ()


def test_s1_07_sl_floor_clip_yields_exactly_67_5bp_tp_no_trade_downstream():
    from rob940_bars_agg import Bar1m
    from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS
    from rob940_engine import run_symbol_stream

    cfg = get_s1_config("S1-07")  # L=16, q_min=1.25, k_SL=1.25, R_TP=1.50
    bars = _flat_warmup(20, h=100.15, low=99.85, c=100.0, v=100.0)
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=100.2,
        high=100.25,
        low=99.95,
        close=100.25,
        volume=125.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert len(signals) == 1
    sig = signals[0]
    assert sig.tp_distance_bps is not None
    assert round(sig.tp_distance_bps, 6) == 67.5
    assert round(sig.sl_distance_bps, 6) == 45.0

    # Feed straight into H2: below the 68bp gate -> no-trade, not silently
    # dropped by the generator itself (S1-07 is retained per Fable Q1=A).
    bars_1m = [
        Bar1m(
            ts=21 * _BUCKET_MS,
            open=100.25,
            high=100.3,
            low=100.2,
            close=100.28,
            volume=1.0,
        )
    ]
    result = run_symbol_stream(bars_1m, signals, COST_SCENARIO_PRIMARY_STRESS)
    assert result.trades == ()
    assert len(result.no_trades) == 1
    assert result.no_trades[0].reason == "tp_below_min_distance"


def test_a_t_below_min_produces_no_signal():
    # I2 (R1 remediation, strategy-verify-rob943-r1-20260717-170045.md): the
    # prior fixture (price ~100, tiny absolute range) was rejected by the
    # CHASE gate, not a_t (chase=0.741>0.50) -- deleting A_T_MIN entirely
    # still yielded no signal, so the lower bound had zero effective
    # coverage. Fixed by holding the ATR-relative shape (and therefore
    # chase/q) IDENTICAL to the passing boundary fixture below while
    # shifting the absolute price level up by a large additive offset
    # (baseline ~1000 instead of ~100): a_t=ATR/C shrinks well under 0.20%
    # while chase=(C-U)/ATR is UNCHANGED (it only depends on price
    # DIFFERENCES, which are untouched by the offset) and q is
    # volume-based, also untouched. a_t is now the ONLY binding gate.
    cfg = get_s1_config("S1-00")
    baseline = 1000.0
    bars = _flat_warmup(20, h=baseline + 0.15, low=baseline - 0.15, c=baseline, v=100.0)
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=baseline + 0.10,
        high=baseline + 0.30,  # =close, no upper wick
        low=baseline + 0.0,
        close=baseline + 0.30,
        volume=125.0,  # q = 1.25 exactly, same as the passing sibling below
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert signals == ()  # a_t ~= 0.0003 (0.03%), well under the 0.20% floor


def test_a_t_lower_boundary_exactly_0_002_emits():
    # Positive control/sibling for the test above: SAME relative shape
    # (chase=0.5, q=1.25 boundaries), but the baseline is chosen so
    # a_t=ATR/C lands EXACTLY on the 0.002 inclusive lower bound -- proving
    # the gate is `>=`, not `>`, and that a regression loosening/removing
    # A_T_MIN would be caught (this fixture would still emit if the lower
    # gate were removed, but the sibling above would ALSO start emitting,
    # which is exactly the mutation this pair is designed to detect).
    cfg = get_s1_config("S1-00")
    baseline = 149.70  # ATR=0.3 (clean), C=150.00 -> a_t=0.3/150.0=0.002 exactly
    bars = _flat_warmup(20, h=baseline + 0.15, low=baseline - 0.15, c=baseline, v=100.0)
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=baseline + 0.10,
        high=baseline + 0.30,
        low=baseline + 0.0,
        close=baseline + 0.30,
        volume=125.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert len(signals) == 1
    assert signals[0].side == "long"


def test_a_t_above_max_produces_no_signal():
    cfg = get_s1_config("S1-00")
    # Very wide flat range -> a_t well over 1.20%.
    bars = _flat_warmup(20, h=104.0, low=96.0, c=100.0, v=100.0)
    breakout = AggregatedBar(
        ts=20 * _BUCKET_MS,
        open=101.0,
        high=105.0,
        low=97.0,
        close=105.0,
        volume=125.0,
        close_ts=21 * _BUCKET_MS,
        is_segment_start=False,
    )
    signals = generate_s1_signals([*bars, breakout], cfg, symbol="XRPUSDT")
    assert signals == ()


def test_unique_signal_ts_per_symbol_fails_closed_on_duplicate():
    from rob940_engine import SignalEvent
    from rob940_signal_s1 import _assert_unique_signal_ts

    dup = (
        SignalEvent(
            strategy="S1",
            config_id="S1-00",
            symbol="XRPUSDT",
            signal_ts=1000,
            side="long",
            sl_distance_bps=50.0,
            tp_distance_bps=90.0,
        ),
        SignalEvent(
            strategy="S1",
            config_id="S1-00",
            symbol="XRPUSDT",
            signal_ts=1000,
            side="short",
            sl_distance_bps=50.0,
            tp_distance_bps=90.0,
        ),
    )
    with pytest.raises(ValueError, match="duplicate"):
        _assert_unique_signal_ts(dup)


# ---------------------------------------------------------------------------
# I4 (R1 remediation): exact frozen-membership fail-closed at the generator
# boundary -- must reject BEFORE any math, even with zero bars.
# ---------------------------------------------------------------------------


def test_generate_s1_signals_rejects_unknown_symbol():
    cfg = get_s1_config("S1-00")
    with pytest.raises(ValueError):
        generate_s1_signals([], cfg, symbol="ETHUSDT")


def test_generate_s1_signals_rejects_forged_unregistered_config():
    forged = S1Config(999, 9.9, 9.9, 9.9, "S1-FORGED", "forged")
    with pytest.raises(ValueError):
        generate_s1_signals([], forged, symbol="XRPUSDT")


def test_generate_s1_signals_rejects_in_domain_param_swapped_config():
    # L swapped 12->24 (both in-domain), config_id left as "S1-01": a caller
    # could otherwise construct this and get silently-computed signals under
    # a registered id that doesn't match its own row.
    swapped = dataclasses.replace(get_s1_config("S1-01"), L=24)
    with pytest.raises(ValueError):
        generate_s1_signals([], swapped, symbol="XRPUSDT")


def test_generate_s1_signals_rejects_hypothesis_tampered_config():
    tampered = dataclasses.replace(get_s1_config("S1-00"), hypothesis="tampered")
    with pytest.raises(ValueError):
        generate_s1_signals([], tampered, symbol="XRPUSDT")


def test_generate_s1_signals_accepts_value_equal_deserialized_config():
    # A freshly-constructed (not `is`-identical) S1Config with EXACTLY the
    # frozen row's values must be accepted -- this is a value-equality
    # check, not an identity check.
    canonical = get_s1_config("S1-00")
    deserialized = S1Config(
        canonical.L,
        canonical.q_min,
        canonical.k_SL,
        canonical.R_TP,
        canonical.config_id,
        canonical.hypothesis,
    )
    assert deserialized is not canonical
    assert deserialized == canonical
    signals = generate_s1_signals([], deserialized, symbol="XRPUSDT")
    assert signals == ()
