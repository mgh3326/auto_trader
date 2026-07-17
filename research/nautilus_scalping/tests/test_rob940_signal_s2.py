"""ROB-943 (H3, ROB-940) — S2 confirmed-shock-reversal-5m signal RED/GREEN tests.

Includes the PERMANENT ambiguity-gate reproduction
(``test_ambiguity_gate_direction_mismatch_final_fable_ruling`` and its
companion GREEN test) per the Fable-approved final ruling in
``orch-fable-answer-rob943-s2-20260717.md`` (Q1=A, direction guard kept,
RED fixture retained permanently — do not delete this test; only its name
changed in the R1 remediation round to drop the stale "pending_consult"
wording -- the ruling itself has been final since 2026-07-17).
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from rob940_bars_agg import AggregatedBar, Bar1m
from rob940_signal_manifest import FrozenSignalConstants, S2Config, get_s2_config
from rob940_signal_s2 import (
    RejectedCandidate,
    _efficiency_ratio,
    _evaluate_target_gates,
    _log_returns,
    _median_mad_sigma,
    count_rejection_reasons,
    generate_s2_signals,
)

_C = FrozenSignalConstants
_BUCKET_MS = 5 * 60_000


def _bar(idx, o, h, low, c, v, *, segment_start=False) -> AggregatedBar:
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


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_log_returns_first_is_none_rest_computed():
    closes = [100.0, 110.0, 99.0]
    r = _log_returns(closes)
    assert r[0] is None
    assert r[1] == pytest.approx(math.log(1.1))
    assert r[2] == pytest.approx(math.log(99.0 / 110.0))


def test_median_mad_sigma_floor_applies_on_zero_mad():
    med, sigma = _median_mad_sigma([0.0, 0.0, 0.0, 0.0, 0.0])
    assert med == 0.0
    assert sigma == 0.0001  # floor, since 1.4826*MAD=0 < floor


def test_median_mad_sigma_hand_verified():
    med, sigma = _median_mad_sigma([-2.0, -1.0, 0.0, 1.0, 2.0])
    assert med == 0.0
    assert sigma == pytest.approx(1.4826 * 1.0)  # MAD = median(|x|) = 1.0


def test_efficiency_ratio_denominator_zero_is_none_fail_closed():
    closes = [5.0, 5.0, 5.0, 5.0, 5.0]
    assert _efficiency_ratio(closes, t=4, window=4) is None


def test_efficiency_ratio_hand_verified():
    closes = [0.0, 1.0, 2.0, 1.0, 0.0]
    # numerator=|closes[4]-closes[0]|=0; denom=1+1+1+1=4
    assert _efficiency_ratio(closes, t=4, window=4) == 0.0


def test_efficiency_ratio_pure_trend_is_one():
    closes = [0.0, 1.0, 2.0, 3.0, 4.0]
    # numerator=4, denom=1+1+1+1=4 -> ER=1.0 (maximally trending)
    assert _efficiency_ratio(closes, t=4, window=4) == 1.0


# ---------------------------------------------------------------------------
# _evaluate_target_gates: direction + magnitude boundary tests
# ---------------------------------------------------------------------------


def test_ambiguity_gate_direction_mismatch_final_fable_ruling():
    """PERMANENT regression (Fable Q1=A final, orch-fable-answer-rob943-s2-
    20260717.md). Reproduces the exact risk described in the consult doc:
    confirmation overshoot puts target T below entry E for a long (or above
    E for a short) even though all THREE magnitude gates independently pass.
    Literal (guard-less) execution would hand H2 a ``tp_target_price`` on
    the wrong side of entry, and H2's ``_gapped_through_tp`` would fire an
    immediate entry-bar "take_profit" exit that is actually a realized
    LOSS (gross_bps negative) mislabeled as a win — corrupting every
    downstream win-rate/PF/timeout metric. The direction guard rejects this
    input with reason ``target_direction_invalid`` instead.
    """
    entry_price = 100.00
    target_price = 99.20  # BELOW entry despite side="long"
    sl_distance = 0.0045  # 45bp
    r_min = 1.25
    passed, reason, d_tp_bps = _evaluate_target_gates(
        "long", entry_price, target_price, sl_distance, r_min
    )
    # Magnitude alone would pass: d_tp=80bp, 68<=80<=120, 80>=1.25*45=56.25.
    assert d_tp_bps == pytest.approx(80.0)
    assert passed is False
    assert reason == "target_direction_invalid"


def test_direction_guard_short_symmetric():
    passed, reason, _ = _evaluate_target_gates("short", 100.00, 100.80, 0.0045, 1.25)
    assert passed is False
    assert reason == "target_direction_invalid"


def test_direction_valid_long_passes_when_magnitude_ok():
    passed, reason, d_tp_bps = _evaluate_target_gates(
        "long", 100.00, 100.80, 0.0045, 1.25
    )
    assert passed is True
    assert reason is None
    assert d_tp_bps == pytest.approx(80.0)


def test_tp_above_120bp_cap_boundary():
    # 120bp exactly passes, 120.01bp fails.
    ok, reason, _ = _evaluate_target_gates("long", 100.00, 101.20, 0.0045, 1.0)
    assert ok is True and reason is None
    bad, reason, _ = _evaluate_target_gates("long", 100.00, 101.2001, 0.0045, 1.0)
    assert bad is False and reason == "tp_above_max"


def test_tp_below_r_min_sl_boundary():
    # d_SL=60bp, R_min=1.25 -> threshold=75bp (above the 68bp abs floor).
    sl = 0.0060
    ok, reason, d_tp = _evaluate_target_gates("long", 100.00, 100.75, sl, 1.25)
    assert ok is True and reason is None
    assert d_tp == pytest.approx(75.0)
    bad, reason, _ = _evaluate_target_gates("long", 100.00, 100.7499, sl, 1.25)
    assert bad is False and reason == "tp_below_r_min_sl"


def test_tp_below_68bp_abs_floor_boundary():
    # d_SL=45bp, R_min=1.20 -> R_min*d_SL=54bp, so 68bp abs floor is binding.
    sl = 0.0045
    ok, reason, d_tp = _evaluate_target_gates("long", 100.00, 100.68, sl, 1.20)
    assert ok is True and reason is None
    assert d_tp == pytest.approx(68.0)
    bad, reason, _ = _evaluate_target_gates("long", 100.00, 100.6799, sl, 1.20)
    assert bad is False and reason == "tp_below_abs_floor"


# ---------------------------------------------------------------------------
# End-to-end generator tests
# ---------------------------------------------------------------------------

_FLAT_N = 288  # prior-288 window; shock evaluated starting bar index 289


def _flat_segment(n: int, *, c=100.0, v=100.0) -> list[AggregatedBar]:
    return [
        _bar(i, c, c, c, c, v, segment_start=(i == 0))  # O=H=L=C, zero TR/return
        for i in range(n)
    ]


def _zigzag_then_shock(
    *,
    shock_close: float,
    confirm_close=None,
    confirm_high=None,
    confirm_low=None,
    flat_n: int = _FLAT_N,
) -> list[AggregatedBar]:
    """``flat_n`` flat bars + 47 alternating +/-0.6 "noise" bars + shock bar
    [+ optional confirm bar]. With the default ``flat_n=288`` the shock lands
    at bar t=335, well above the 289 min-eligible-index floor (ample margin
    for window slicing). Passing ``flat_n=241``/``242`` places the shock
    exactly at the t=288 (excluded) / t=289 (first eligible) boundary.
    """
    bars = _flat_segment(flat_n)
    noise: list[AggregatedBar] = []
    prev_close = 100.0
    for k in range(47):
        idx = flat_n + k
        c = 100.6 if k % 2 == 0 else 100.0
        noise.append(
            _bar(idx, prev_close, max(prev_close, c), min(prev_close, c), c, 100.0)
        )
        prev_close = c
    shock_idx = flat_n + 47
    shock_bar = _bar(
        shock_idx,
        prev_close,
        max(prev_close, shock_close),
        min(prev_close, shock_close),
        shock_close,
        250.0,  # v_t/median(100)=2.5, comfortably >= any v_min domain value
    )
    out = [*bars, *noise, shock_bar]
    if confirm_close is not None:
        confirm_bar = _bar(
            shock_idx + 1,
            shock_close,
            confirm_high,
            confirm_low,
            confirm_close,
            100.0,
        )
        out.append(confirm_bar)
    return out


def test_shock_negative_confirmed_emits_long_signal_next_bar_only():
    cfg = get_s2_config("S2-00")  # z_min=3.00, v_min=2.00, ER_max=0.35, R_min=1.25
    shock_close = 99.9  # prev_close(=100.6) -> r_t ~ -0.696%
    bars = _zigzag_then_shock(
        shock_close=shock_close,
        confirm_close=100.2,
        confirm_high=100.25,
        confirm_low=99.95,  # >= shock bar's low (99.9)
    )
    # E chosen so d_TP=|T/E-1| lands comfortably inside [68,120]bp with
    # T=100.6 (=C_{t-1}, the shock bar's own prior close) and T>E (long-
    # valid direction): 100.6/99.70-1 = 90.27bp.
    bars_1m = [
        Bar1m(
            ts=bars[-1].close_ts,
            open=99.70,
            high=99.9,
            low=99.6,
            close=99.8,
            volume=10.0,
        )
    ]
    result = generate_s2_signals(bars, bars_1m, cfg, symbol="XRPUSDT")
    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.side == "long"
    assert sig.strategy == "S2"
    assert sig.config_id == "S2-00"
    # signal_ts is the CONFIRMATION bar's close, never the shock bar's own close.
    assert sig.signal_ts == bars[-1].close_ts
    assert sig.signal_ts != bars[-2].close_ts  # shock bar itself never fires
    assert sig.timeout_bars == 30
    assert sig.cooldown_bars == 60
    assert sig.tp_target_price == pytest.approx(
        100.6
    )  # T = C_{t-1} (shock's prior close)
    assert sig.tp_distance_bps is None
    assert sig.sl_distance_bps == pytest.approx(45.0)  # 0.60*|r_t|=41.9bp < floor


def test_exact_t_plus_1_only_no_retry_at_t_plus_2():
    cfg = get_s2_config("S2-00")
    shock_close = 99.9
    bars = _zigzag_then_shock(shock_close=shock_close)  # no confirmation bar appended
    shock_idx = _FLAT_N + 47
    # t+1 FAILS confirmation (price keeps falling, doesn't recover).
    fail_confirm = _bar(shock_idx + 1, 99.9, 99.9, 99.5, 99.6, 100.0)
    # t+2 WOULD satisfy the (would-be) confirmation shape vs the shock bar
    # (C=100.4>99.9=shock close, L=99.95>=99.9=shock low), but must never
    # be considered -- only t+1 is eligible.
    would_confirm = _bar(shock_idx + 2, 99.95, 100.5, 99.95, 100.4, 100.0)
    bars = [*bars, fail_confirm, would_confirm]
    result = generate_s2_signals(bars, [], cfg, symbol="XRPUSDT")
    assert result.signals == ()
    reasons = count_rejection_reasons(result.rejections)
    assert reasons.get("confirmation_failed", 0) == 1


def test_next_bar_unavailable_is_rejected_not_silently_dropped():
    cfg = get_s2_config("S2-00")
    bars = _zigzag_then_shock(
        shock_close=99.9, confirm_close=100.2, confirm_high=100.25, confirm_low=99.95
    )
    # No 1m bars at all -> E cannot be resolved.
    result = generate_s2_signals(bars, [], cfg, symbol="XRPUSDT")
    assert result.signals == ()
    reasons = count_rejection_reasons(result.rejections)
    assert reasons.get("next_bar_unavailable", 0) == 1


def test_rejection_dataclass_has_reason_field_for_aggregation():
    rc = RejectedCandidate(
        strategy="S2",
        config_id="S2-00",
        symbol="XRPUSDT",
        signal_ts=1000,
        side="long",
        reason="target_direction_invalid",
    )
    assert count_rejection_reasons((rc, rc)) == {"target_direction_invalid": 2}


# ---------------------------------------------------------------------------
# I4 (R1 remediation): exact frozen-membership fail-closed at the generator
# boundary -- must reject BEFORE any math, even with zero bars.
# ---------------------------------------------------------------------------


def test_generate_s2_signals_rejects_unknown_symbol():
    cfg = get_s2_config("S2-00")
    with pytest.raises(ValueError):
        generate_s2_signals([], [], cfg, symbol="ETHUSDT")


def test_generate_s2_signals_rejects_forged_unregistered_config():
    forged = S2Config(9.9, 9.9, 9.9, 9.9, "S2-FORGED", "forged")
    with pytest.raises(ValueError):
        generate_s2_signals([], [], forged, symbol="XRPUSDT")


def test_generate_s2_signals_rejects_in_domain_param_swapped_config():
    swapped = dataclasses.replace(get_s2_config("S2-01"), z_min=3.25)
    with pytest.raises(ValueError):
        generate_s2_signals([], [], swapped, symbol="XRPUSDT")


def test_generate_s2_signals_rejects_hypothesis_tampered_config():
    tampered = dataclasses.replace(get_s2_config("S2-00"), hypothesis="tampered")
    with pytest.raises(ValueError):
        generate_s2_signals([], [], tampered, symbol="XRPUSDT")


def test_generate_s2_signals_accepts_value_equal_deserialized_config():
    canonical = get_s2_config("S2-00")
    deserialized = S2Config(
        canonical.z_min,
        canonical.v_min,
        canonical.ER_max,
        canonical.R_min,
        canonical.config_id,
        canonical.hypothesis,
    )
    assert deserialized is not canonical
    assert deserialized == canonical
    result = generate_s2_signals([], [], deserialized, symbol="XRPUSDT")
    assert result.signals == ()
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# I3 (R1 remediation): non-vacuous gap-reset -- a REAL 1m bar is present at
# the confirmation close_ts so the is_segment_start=False control genuinely
# emits, proving the True (gap) branch's 0-signal/0-rejection result is due
# to the reset discarding a stale pending shock, not an unrelated no-op.
# ---------------------------------------------------------------------------


def _gap_reset_fixture(*, gap: bool) -> tuple[list[AggregatedBar], list[Bar1m]]:
    bars = _zigzag_then_shock(
        shock_close=99.9, confirm_close=100.2, confirm_high=100.25, confirm_low=99.95
    )
    # Flip only the confirmation bar's is_segment_start flag: `gap=True`
    # means a real time discontinuity occurred right before the would-be
    # confirmation bar, so it starts a NEW segment and the shock (last bar
    # of the OLD segment) can never be confirmed against it.
    confirm = bars[-1]
    bars[-1] = dataclasses.replace(confirm, is_segment_start=gap)
    bars_1m = [
        Bar1m(
            ts=confirm.close_ts,
            open=99.70,
            high=99.9,
            low=99.6,
            close=99.8,
            volume=10.0,
        )
    ]
    return bars, bars_1m


def test_gap_reset_control_no_gap_emits_real_signal():
    bars, bars_1m = _gap_reset_fixture(gap=False)
    result = generate_s2_signals(
        bars, bars_1m, get_s2_config("S2-00"), symbol="XRPUSDT"
    )
    assert len(result.signals) == 1
    assert result.signals[0].side == "long"


def test_gap_reset_discards_pending_shock_stale_candidate_never_evaluated():
    bars, bars_1m = _gap_reset_fixture(gap=True)
    result = generate_s2_signals(
        bars, bars_1m, get_s2_config("S2-00"), symbol="XRPUSDT"
    )
    assert result.signals == ()
    # Not merely "no signal" -- the pending shock must never have been
    # EVALUATED at all (no confirmation_failed/next_bar_unavailable/etc.
    # rejection row either), proving it was discarded by the reset rather
    # than considered and rejected for some unrelated reason.
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# Recommended regressions (R1's independently-verified one-offs, committed)
# ---------------------------------------------------------------------------


def test_shock_at_t_288_excluded_t_289_first_eligible():
    cfg = get_s2_config("S2-00")
    # t=288: flat_n=241 -> shock_idx=241+47=288 (one BELOW the 289 floor).
    excluded = _zigzag_then_shock(shock_close=99.9, flat_n=241)
    result_excluded = generate_s2_signals(excluded, [], cfg, symbol="XRPUSDT")
    assert result_excluded.signals == ()
    assert result_excluded.rejections == ()  # never evaluated, not rejected

    # t=289: flat_n=242 -> shock_idx=242+47=289 (first eligible), with a
    # real confirmation + 1m bar so a genuine signal proves it WAS evaluated.
    included = _zigzag_then_shock(
        shock_close=99.9,
        flat_n=242,
        confirm_close=100.2,
        confirm_high=100.25,
        confirm_low=99.95,
    )
    bars_1m = [
        Bar1m(
            ts=included[-1].close_ts,
            open=99.70,
            high=99.9,
            low=99.6,
            close=99.8,
            volume=10.0,
        )
    ]
    result_included = generate_s2_signals(included, bars_1m, cfg, symbol="XRPUSDT")
    assert len(result_included.signals) == 1


def test_positive_shock_short_confirmation_full_signal_round_trips_through_h2():
    from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS
    from rob940_engine import run_symbol_stream

    cfg = get_s2_config("S2-00")
    shock_close = 101.3  # prev_close=100.6 -> r_t=+0.693% (positive shock)
    bars = _zigzag_then_shock(
        shock_close=shock_close,
        confirm_close=101.0,
        confirm_high=101.2,  # <= shock high (101.3)
        confirm_low=100.9,
    )
    # T=C_{t-1}=100.6 (shock's own prior close); E=101.50 puts T<E, valid
    # for short. d_TP=|100.6/101.50-1|=88.67bp, within [68,120]bp.
    bars_1m = [
        Bar1m(
            ts=bars[-1].close_ts,
            open=101.50,
            high=101.6,
            low=101.4,
            close=101.55,
            volume=10.0,
        )
    ]
    result = generate_s2_signals(bars, bars_1m, cfg, symbol="XRPUSDT")
    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.side == "short"
    assert sig.tp_target_price == pytest.approx(100.6)

    engine_bars_1m = [
        Bar1m(
            ts=bars_1m[0].ts,
            open=101.50,
            high=101.55,
            low=101.30,
            close=101.35,
            volume=1.0,
        )
    ]
    engine_result = run_symbol_stream(
        engine_bars_1m, result.signals, COST_SCENARIO_PRIMARY_STRESS
    )
    assert len(engine_result.trades) == 1
    assert engine_result.trades[0].side == "short"


def test_next_bar_unavailable_does_not_scan_forward_to_a_later_1m_bar():
    cfg = get_s2_config("S2-00")
    bars = _zigzag_then_shock(
        shock_close=99.9, confirm_close=100.2, confirm_high=100.25, confirm_low=99.95
    )
    confirm_close_ts = bars[-1].close_ts
    # A 1m bar exists, but one minute LATER than the exact required ts --
    # the engine-consumption contract (AC3) forbids searching further ahead.
    later_only = [
        Bar1m(
            ts=confirm_close_ts + 60_000,
            open=99.70,
            high=99.9,
            low=99.6,
            close=99.8,
            volume=10.0,
        )
    ]
    result = generate_s2_signals(bars, later_only, cfg, symbol="XRPUSDT")
    assert result.signals == ()
    reasons = count_rejection_reasons(result.rejections)
    assert reasons.get("next_bar_unavailable", 0) == 1


def test_real_pipeline_target_direction_invalid_reason_count():
    """FABLE-A1 gap closed: aggregate a rejection produced by an actual
    ``generate_s2_signals`` run (not a hand-built ``RejectedCandidate``).
    """
    cfg = get_s2_config("S2-00")
    bars = _zigzag_then_shock(
        shock_close=99.9, confirm_close=100.2, confirm_high=100.25, confirm_low=99.95
    )
    # E=101.0 puts E ABOVE T=100.6 for a long -> direction-invalid, even
    # though the shock/confirmation themselves are perfectly valid.
    bars_1m = [
        Bar1m(
            ts=bars[-1].close_ts,
            open=101.0,
            high=101.1,
            low=100.9,
            close=101.05,
            volume=10.0,
        )
    ]
    result = generate_s2_signals(bars, bars_1m, cfg, symbol="XRPUSDT")
    assert result.signals == ()
    assert count_rejection_reasons(result.rejections) == {"target_direction_invalid": 1}
