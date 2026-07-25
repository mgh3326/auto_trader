"""ROB-943 (H3, ROB-940) — S2 confirmed-shock-reversal-5m signal generator
(pure, stdlib).

Implements the ROB-940 research draft's Strategy 2: a large-shock detector
(prior-288 return median/MAD z-score, volume spike, efficiency-ratio regime
filter) that never enters on the shock bar itself, only on an EXACT next-bar
confirmation, with a target-validity gate evaluated against the ACTUAL next
contiguous 1m open ``E`` (per the ROB-943 prompt's H2-consumption contract —
H2 does not enforce Strategy-2-specific TP bounds, so this module must
evaluate them itself before emitting any ``SignalEvent``).

ultrathink (ER denominator=0): a zero denominator means the 48-bar window
had ZERO net movement across every consecutive pair — a degenerate/flat
input, not a malformed one. Raising would kill the whole run on ordinary
low-liquidity data; per the ROB-943 prompt's "명시적 deterministic no-signal"
allowance, ``_efficiency_ratio`` returns ``None`` and the caller treats that
as "ER gate fails" (no shock), never as NaN/silent-pass.

ultrathink (S2 target-direction ambiguity — FINAL, Fable Q1=A):
``orch-fable-answer-rob943-s2-20260717.md`` confirms the direction guard in
``_evaluate_target_gates`` (T must be on the profit side of E for the given
side) as the permanent behavior, not a placeholder. Economic rationale: the
S2 thesis is literally "price reverts to the pre-shock close T"; if
confirmation-bar overshoot already carried E past T, the reversion thesis is
already exhausted and passing the trade to H2 unguarded produces a
mislabeled entry-bar "take_profit" exit that is actually a realized loss
(see ``tests/test_rob940_signal_s2.py::
test_ambiguity_gate_direction_mismatch_final_fable_ruling``, kept
permanently per the ruling's condition 3). Spec-deviation register entry
(Fable condition 2, do not re-word): see ``SPEC_DEVIATIONS`` below.

ultrathink (I4, ROB-943 R1 remediation): ``get_s2_config`` only fails closed
for callers that go THROUGH it. ``generate_s2_signals`` now asserts exact
frozen-manifest membership (symbol + config, by VALUE not identity) as its
very first act, before touching ``bars_5m``/``bars_1m`` at all.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports —
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from rob940_bars_agg import AggregatedBar, Bar1m
from rob940_engine import SignalEvent
from rob940_signal_manifest import (
    FrozenSignalConstants,
    S2Config,
    _validate_symbol,
    assert_matches_frozen_s2_config,
)
from rob940_signal_s1 import _assert_unique_signal_ts, _segment_slices

_C = FrozenSignalConstants

# Fable condition 2 (orch-fable-answer-rob943-s2-20260717.md): the exact
# spec-deviation text for the future H5 scorecard register. Do not implement
# H5 here -- this is the citation payload only.
SPEC_DEVIATIONS: tuple[str, ...] = (
    "원문 3게이트에 방향 유효성 게이트 추가(Fable 판정, 사유: 라벨 오염 차단)",
)


@dataclass(frozen=True)
class RejectedCandidate:
    strategy: str
    config_id: str
    symbol: str
    signal_ts: int
    side: str
    reason: str
    fold_id: str | None = None


@dataclass(frozen=True)
class S2GenerationResult:
    signals: tuple[SignalEvent, ...]
    rejections: tuple[RejectedCandidate, ...]


def count_rejection_reasons(rejections: Sequence[RejectedCandidate]) -> dict[str, int]:
    """Fable condition 1: aggregate rejection reasons (incl.
    ``target_direction_invalid``) so H5 can surface frequency. Counting
    only -- no H5 reporting pipeline is implemented here.
    """
    counts: dict[str, int] = {}
    for r in rejections:
        counts[r.reason] = counts.get(r.reason, 0) + 1
    return counts


def _log_returns(closes: Sequence[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        out[i] = math.log(closes[i] / closes[i - 1])
    return out


def _median_mad_sigma(window: Sequence[float]) -> tuple[float, float]:
    med = statistics.median(window)
    mad = statistics.median(abs(x - med) for x in window)
    sigma = max(1.4826 * mad, 0.0001)
    return med, sigma


def _efficiency_ratio(closes: Sequence[float], t: int, window: int) -> float | None:
    """ER48 over ``closes[t-window .. t]`` (window+1 points, window diffs).

    Returns ``None`` (fail-closed no-shock sentinel) if the path-length
    denominator is exactly zero.
    """
    numerator = abs(closes[t] - closes[t - window])
    denom = sum(abs(closes[j] - closes[j - 1]) for j in range(t - window + 1, t + 1))
    if denom == 0:
        return None
    return numerator / denom


def _evaluate_target_gates(
    side: str, entry_price: float, target_price: float, sl_distance: float, r_min: float
) -> tuple[bool, str | None, float]:
    """The three magnitude gates PLUS the direction-validity guard (final,
    see module docstring). Returns ``(passed, rejection_reason, d_tp_bps)``.
    """
    # Rounded to 1e-8 bps to strip binary-float representation noise (e.g.
    # 101.20/100.00-1 lands a few ULPs past 0.012) without masking any real
    # boundary distinction -- inclusive gates below are exact-decimal spec
    # boundaries (68/120bp, R_min*d_SL), not float-noise-sensitive ones.
    # M3 (ROB-943 R1 remediation): the R_min*d_SL threshold gets the SAME
    # rounding treatment as d_tp_bps -- rounding only the LHS left an
    # asymmetric ~1e-14bp ULP gap (e.g. R_min=1.20/d_SL=90bp -> raw RHS
    # 107.99999999999999 vs exact 108.0); harmless in practice (always
    # permissive, dwarfed by the 1e-8bp LHS rounding) but a needless
    # inconsistency once one side was already being cleaned up.
    d_tp_bps = round(abs(target_price / entry_price - 1.0) * 1e4, 8)
    r_min_sl_bps = round(r_min * sl_distance * 1e4, 8)
    if side == "long" and not (target_price > entry_price):
        return False, "target_direction_invalid", d_tp_bps
    if side == "short" and not (target_price < entry_price):
        return False, "target_direction_invalid", d_tp_bps
    if d_tp_bps > _C.S2_TP_MAX_BPS:
        return False, "tp_above_max", d_tp_bps
    if d_tp_bps < r_min_sl_bps:
        return False, "tp_below_r_min_sl", d_tp_bps
    if d_tp_bps < _C.S2_TP_ABS_FLOOR_BPS:
        return False, "tp_below_abs_floor", d_tp_bps
    return True, None, d_tp_bps


def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def generate_s2_signals(
    bars_5m: Sequence[AggregatedBar],
    bars_1m: Sequence[Bar1m],
    config: S2Config,
    *,
    symbol: str,
    fold_id: str | None = None,
) -> S2GenerationResult:
    """Generate the S2 (confirmed-shock-reversal-5m) stream for ONE symbol/
    config. ``bars_1m`` is the raw 1m stream used ONLY to resolve the actual
    next contiguous 1m open ``E`` for the pre-signal TP-validity gates —
    this module never touches execution/cost/arbitration logic.
    """
    _validate_symbol(symbol)
    assert_matches_frozen_s2_config(config)

    signals: list[SignalEvent] = []
    rejections: list[RejectedCandidate] = []
    bars_1m_by_ts = {b.ts: b for b in bars_1m}
    window = _C.S2_MAD_WINDOW  # 288
    er_window = _C.S2_ER_WINDOW  # 48
    min_idx = window + 1  # need window PRIOR returns => t >= window+1 (289)

    for seg_start, seg_stop in _segment_slices(bars_5m):
        seg = bars_5m[seg_start:seg_stop]
        closes = [b.close for b in seg]
        volumes = [b.volume for b in seg]
        returns = _log_returns(closes)

        pending_t: int | None = None  # index of a shock awaiting t+1 confirmation
        pending_direction: str | None = None  # "long" | "short"

        for t in range(1, len(seg)):
            if pending_t is not None and t == pending_t + 1:
                shock_bar = seg[pending_t]
                confirm_bar = seg[t]
                if pending_direction == "long":
                    confirmed = (
                        confirm_bar.close > shock_bar.close
                        and confirm_bar.low >= shock_bar.low
                    )
                else:
                    confirmed = (
                        confirm_bar.close < shock_bar.close
                        and confirm_bar.high <= shock_bar.high
                    )
                signal_ts = confirm_bar.close_ts
                if not confirmed:
                    rejections.append(
                        RejectedCandidate(
                            strategy="S2",
                            config_id=config.config_id,
                            symbol=symbol,
                            # Confirmation failure becomes observable only
                            # after this confirmation bar closes.  Use the
                            # same decision-time authority as every other
                            # S2 outcome so a bar that is itself the next
                            # shock cannot collide with this rejection.
                            signal_ts=signal_ts,
                            side=pending_direction,
                            reason="confirmation_failed",
                            fold_id=fold_id,
                        )
                    )
                else:
                    entry_bar = bars_1m_by_ts.get(signal_ts)
                    if entry_bar is None:
                        rejections.append(
                            RejectedCandidate(
                                strategy="S2",
                                config_id=config.config_id,
                                symbol=symbol,
                                signal_ts=signal_ts,
                                side=pending_direction,
                                reason="next_bar_unavailable",
                                fold_id=fold_id,
                            )
                        )
                    else:
                        r_shock = returns[pending_t]
                        shock_bar_prev_close = seg[pending_t - 1].close
                        entry_price = entry_bar.open
                        d_sl = _clip(
                            0.60 * abs(r_shock),
                            _C.S2_SL_CLIP_MIN_BPS / 1e4,
                            _C.S2_SL_CLIP_MAX_BPS / 1e4,
                        )
                        passed, reason, _d_tp_bps = _evaluate_target_gates(
                            pending_direction,
                            entry_price,
                            shock_bar_prev_close,
                            d_sl,
                            config.R_min,
                        )
                        if not passed:
                            rejections.append(
                                RejectedCandidate(
                                    strategy="S2",
                                    config_id=config.config_id,
                                    symbol=symbol,
                                    signal_ts=signal_ts,
                                    side=pending_direction,
                                    reason=reason,
                                    fold_id=fold_id,
                                )
                            )
                        else:
                            signals.append(
                                SignalEvent(
                                    strategy="S2",
                                    config_id=config.config_id,
                                    symbol=symbol,
                                    signal_ts=signal_ts,
                                    side=pending_direction,
                                    sl_distance_bps=d_sl * 1e4,
                                    tp_target_price=shock_bar_prev_close,
                                    timeout_bars=_C.S2_TIMEOUT_1M_BARS,
                                    cooldown_bars=_C.S2_COOLDOWN_1M_BARS,
                                    fold_id=fold_id,
                                )
                            )
                pending_t = None
                pending_direction = None
                # A shock is evaluated at most once (per-shock rule); the
                # confirmation bar itself is still independently checked
                # below for whether IT is a new shock.

            if t < min_idx or t < er_window:
                continue
            r_t = returns[t]
            if r_t is None or not math.isfinite(r_t):
                continue
            if abs(r_t) < _C.S2_SHOCK_ABS_RETURN_MIN:
                continue

            ret_window = returns[t - window : t]
            if any(x is None for x in ret_window):
                continue
            median_r, sigma = _median_mad_sigma(ret_window)  # type: ignore[arg-type]
            z_t = (r_t - median_r) / sigma
            if abs(z_t) < config.z_min:
                continue

            vol_window = volumes[t - window : t]
            vol_median = statistics.median(vol_window)
            if not math.isfinite(vol_median) or vol_median <= 0:
                continue
            v_t = seg[t].volume / vol_median
            if v_t < config.v_min:
                continue

            er = _efficiency_ratio(closes, t, er_window)
            if er is None or er > config.ER_max:
                continue

            # All shock conditions satisfied; sign of r_t determines
            # direction (never 0 here since |r_t|>=0.006 already excludes
            # it). Negative shock -> long confirmation; positive -> short.
            pending_t = t
            pending_direction = "long" if r_t < 0 else "short"

    result_signals = tuple(signals)
    _assert_unique_signal_ts(result_signals)
    return S2GenerationResult(signals=result_signals, rejections=tuple(rejections))
