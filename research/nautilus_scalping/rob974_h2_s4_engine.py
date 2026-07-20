"""ROB-979 (H2, ROB-974 R2) CP3 -- S4 historical pair-basket engine (pure, stdlib).

ONE global pair basket at a time (ROB-979 AC14): both legs enter atomically
at the SAME synchronized minute open; if either leg lacks that exact tick the
whole pair is NO_TRADE (never a partial one-leg fill).

ultrathink decisions (frozen for CP3-CP5; revisit only if orch authority
changes -- see ``/tmp/strategy-worker-rob979-sonnet-checkpoints.md`` CP3 entry):

  * Signed basket-return formula: for each leg ``i`` with entry-frozen weight
    ``w_i`` and side, ``k_i = w_i * (+1 if long else -1)``, and
    ``G(P_a,P_b) = k_a*ln(P_a/E_a) + k_b*ln(P_b/E_b)``. This generalizes the
    research brief's ``z_entry>0`` example (short a/long b:
    ``G = -w_a*ln(Pa/Ea) + w_b*ln(Pb/Eb)``) to both entry directions without
    a separate sign branch.
  * G_min/G_max CONSERVATIVE BOUND (AC18) is a DIFFERENT quantity from the
    entry-sizing gross notional G (AC17) despite the shared name in the
    authority doc -- this module keeps them lexically distinct
    (``_g_bounds_notional`` for AC17's sizing feasibility check,
    ``_conservative_bounds`` for AC18/19's per-minute worst/best-case basket
    RETURN bound). Per leg, the worst-case (G_min) bound uses that leg's LOW
    if its coefficient ``k_i`` is positive and HIGH if negative (the price
    that makes ``k_i*ln(P/E)`` as small as possible); G_max uses the reverse.
    This is a valid lower/upper bound on the ACTUAL basket return at every
    instant within the minute, for ANY intra-minute price path of either leg,
    because it is evaluated independently per leg at that leg's own extremum.
  * AC19's asymmetric SL/TP recognition, read literally: SL is credited on
    "the adverse bound CAN touch" it (``G_min_bound <= -d_SL`` -- ANY
    possibility of that much adverse movement is treated as certain, the
    same conservative-SL convention used throughout this repo) but TP is
    credited only when "the worst-case bound is already at/above TP"
    (``G_min_bound >= d_TP`` -- i.e. TP is GUARANTEED regardless of how the
    two legs' intra-minute paths actually interleaved, not merely possible).
    G_max_bound is therefore NEVER used as a trigger -- only as the MFE
    (best-possible favorable excursion) contribution; SL/MAE and the sole TP
    trigger both come from G_min_bound. This is deliberately asymmetric: it
    biases toward recognizing losses and away from claiming unverified gains
    in a two-leg pseudo-atomic historical simulation (consistent with the
    module's broader historical-null posture).
  * Gap fill values follow the SAME real-vs-barrell convention as S3/ROB-940:
    a gap-SL fills at the REAL (uncapped, computed from the actual open
    prices) G_open -- which is necessarily worse than -d_SL -- while a
    gap-TP fills CAPPED at exactly +d_TP (no windfall). Conservative-bound
    (non-gap) SL/TP always fill AT the barrier (-d_SL/+d_TP), never at the
    raw bound value, mirroring S3's intrabar-touch convention.
  * MEAN_EXIT/STALL_EXIT use the CANONICAL (side-independent) beta-neutral
    log-spread ``s_ab,tau = w_a*ln(C_a,tau) - w_b*ln(C_b,tau)`` (fixed
    minus-sign convention, NOT the side-signed ``k_a/k_b`` used for G) against
    the entry-FROZEN ``mu``/``sigma``, using the ``S4PairLegClose.close``
    values at each completed-4h boundary -- a DIFFERENT input than the
    G-bound calculations, which use the SAME boundary minute's own
    ``MinuteBar`` OHLC. These two inputs are trusted independently and never
    cross-validated against each other (H1 supplies both; H2 is not the
    reconciliation authority for whether a close feature numerically matches
    its corresponding minute-bar open).
  * STALL_EXIT eligibility ("after 2 completed 4h bars") is read as: the
    check is ACTIVE starting at the boundary whose ``k==2`` (exactly 8h after
    entry, matching the research brief's own "8h 동안 15%도 수렴 실패" framing)
    and every boundary thereafter -- not "only after the 2nd boundary has
    already passed" (which would start eligibility at k==3).
  * S4 has NO per-pair cooldown and NO daily entry-count/SL-halt gates,
    UNLIKE S3. ROB-979's S4 acceptance criteria (AC14-24) never mention them
    -- only "permit only one global pair basket at a time" (AC14) and the
    same-tick arbitration rule (AC21). Inventing S3-style day gates for S4
    would be unauthorized semantics; the only active gate here is the
    single-open-basket constraint, via the same
    ``candidate.signal_ts < position_exit_ts`` half-open-window mechanism
    used in ``rob974_h2_s3_engine``.
  * AC17's defensive G re-validation happens BEFORE exact-tick entry
    resolution (a cheap structural check on the intent itself, independent
    of any market data) and rejects with ``g_infeasible`` (``G_min>G_max``)
    or ``g_mismatch`` (a feasible bound whose deterministic ``G_min`` choice
    does not match the intent's supplied ``gross_notional``) -- H2 REJECTS a
    mismatched intent, it never silently substitutes its own recomputed
    value (H2 does not re-estimate/override H3's entry-frozen values, AC33).
  * EOF/gap/horizon 4-way split mirrors ``rob974_h2_s3_engine`` exactly
    (including D1's inclusive ``horizon_end_ts`` equality: the check is
    ``next_ts > horizon_end_ts``, never ``>=``, so an exact
    ``signal_ts + strategy_max_hold == phase_end`` boundary is still
    readable/evaluable), but a gap in EITHER leg's minute bar is terminal
    ``data_gap_in_pair_position`` (AC23) -- no rehedge, no forward-fill, no
    single-leg continuation.
  * MFE/MAE capping (AC26) mirrors S3: every non-exit minute (including the
    entry minute and any boundary minute that fell through to the bounds
    check) contributes BOTH ``G_min_bound`` and ``G_max_bound`` (bps) as
    candidates; the exit minute contributes exactly the ONE actual fill value
    (gap-real, barrier-capped, or the boundary-open administrative fill).

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rob974_h2_dtos import (
    PAIR_EXEC_FAIL_NOT_EVALUATED,
    PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
    MinuteBar,
    S4EngineResult,
    S4IncompleteRecord,
    S4NoTradeRecord,
    S4PairLegClose,
    S4PairSignalIntent,
    S4PairTrade,
)
from rob974_h2_ingress import MinuteIndex, resolve_entry_minute

_MIN_MS = 60_000
FOUR_H_MS = 4 * 3_600_000
MAX_HOLD_BARS = 9  # 9 completed 4h bars == 36h
MAX_HOLD_MS = MAX_HOLD_BARS * FOUR_H_MS
STALL_ELIGIBLE_FROM_BOUNDARY = 2
MEAN_EXIT_Z_THRESHOLD = 0.25
STALL_EXIT_Z_FRACTION = 0.85
_G_LEG_MIN_USD = 6.0
_G_LEG_MAX_USD = 10.0
_G_TOLERANCE = 1e-9

PairCloseIndex = Mapping[tuple[str, int], S4PairLegClose]


def _g_bounds_notional(weight_a: float, weight_b: float) -> tuple[float, float]:
    """AC17's entry-sizing gross-notional bounds (dollars), NOT the per-minute
    conservative basket-return bound (``_conservative_bounds``, AC18/19)."""
    g_min = max(_G_LEG_MIN_USD / weight_a, _G_LEG_MIN_USD / weight_b)
    g_max = min(_G_LEG_MAX_USD / weight_a, _G_LEG_MAX_USD / weight_b)
    return g_min, g_max


def _leg_coefficients(cand: S4PairSignalIntent) -> tuple[float, float]:
    k_a = cand.weight_a * (1.0 if cand.side_a == "long" else -1.0)
    k_b = cand.weight_b * (1.0 if cand.side_b == "long" else -1.0)
    return k_a, k_b


def _basket_return(
    k_a: float, k_b: float, p_a: float, e_a: float, p_b: float, e_b: float
) -> float:
    return k_a * math.log(p_a / e_a) + k_b * math.log(p_b / e_b)


def _conservative_bounds(
    k_a: float, k_b: float, bar_a: MinuteBar, bar_b: MinuteBar, e_a: float, e_b: float
) -> tuple[float, float]:
    """Return ``(g_min_bound, g_max_bound)`` for this minute -- AC18/19."""

    def _bound_price(bar: MinuteBar, k: float, want_min: bool) -> float:
        use_low = (k > 0) if want_min else (k <= 0)
        return bar.low if use_low else bar.high

    p_a_min = _bound_price(bar_a, k_a, want_min=True)
    p_b_min = _bound_price(bar_b, k_b, want_min=True)
    p_a_max = _bound_price(bar_a, k_a, want_min=False)
    p_b_max = _bound_price(bar_b, k_b, want_min=False)
    g_min = _basket_return(k_a, k_b, p_a_min, e_a, p_b_min, e_b)
    g_max = _basket_return(k_a, k_b, p_a_max, e_a, p_b_max, e_b)
    return g_min, g_max


@dataclass
class _ExtremaTracker:
    mfe_bps: float = 0.0
    mae_bps: float = 0.0

    def observe(self, value_bps: float) -> None:
        if value_bps > self.mfe_bps:
            self.mfe_bps = value_bps
        if value_bps < self.mae_bps:
            self.mae_bps = value_bps


@dataclass
class _S4ExitOutcome:
    exit_ts: int
    exit_price_a: float
    exit_price_b: float
    gross_bps: float
    exit_reason: str
    mfe_bps: float
    mae_bps: float


@dataclass
class _S4Incomplete:
    reason: str
    entry_ts: int
    entry_price_a: float
    entry_price_b: float


def _walk_s4_position(
    cand: S4PairSignalIntent,
    entry_bar_a: MinuteBar,
    entry_bar_b: MinuteBar,
    minute_index: MinuteIndex,
    pair_close_index: PairCloseIndex,
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None,
) -> _S4ExitOutcome | _S4Incomplete:
    symbol_a, symbol_b = cand.pair
    entry_ts = entry_bar_a.open_time
    e_a, e_b = entry_bar_a.open, entry_bar_b.open
    k_a, k_b = _leg_coefficients(cand)
    d_sl, d_tp = cand.entry_sl_distance, cand.entry_tp_distance
    tracker = _ExtremaTracker()

    cur_a, cur_b = entry_bar_a, entry_bar_b

    while True:
        is_boundary = (
            cur_a.open_time > entry_ts and (cur_a.open_time - entry_ts) % FOUR_H_MS == 0
        )
        boundary_k = (cur_a.open_time - entry_ts) // FOUR_H_MS if is_boundary else 0

        g_open = _basket_return(k_a, k_b, cur_a.open, e_a, cur_b.open, e_b)
        if g_open <= -d_sl:
            tracker.observe(g_open * 1e4)
            return _S4ExitOutcome(
                cur_a.open_time,
                cur_a.open,
                cur_b.open,
                g_open * 1e4,
                "SL",
                tracker.mfe_bps,
                tracker.mae_bps,
            )
        gap_tp = g_open >= d_tp

        if is_boundary:
            if gap_tp:
                tracker.observe(d_tp * 1e4)
                return _S4ExitOutcome(
                    cur_a.open_time,
                    cur_a.open,
                    cur_b.open,
                    d_tp * 1e4,
                    "TP",
                    tracker.mfe_bps,
                    tracker.mae_bps,
                )
            feat_a = pair_close_index.get((symbol_a, cur_a.open_time))
            feat_b = pair_close_index.get((symbol_b, cur_a.open_time))
            if feat_a is None or feat_b is None:
                return _S4Incomplete("missing_future_data", entry_ts, e_a, e_b)
            s_ab = cand.weight_a * math.log(feat_a.close) - cand.weight_b * math.log(
                feat_b.close
            )
            z_frozen = (s_ab - cand.mu) / max(cand.sigma, 1e-6)
            admin_g = _basket_return(k_a, k_b, cur_a.open, e_a, cur_b.open, e_b)
            if abs(z_frozen) <= MEAN_EXIT_Z_THRESHOLD:
                tracker.observe(admin_g * 1e4)
                return _S4ExitOutcome(
                    cur_a.open_time,
                    cur_a.open,
                    cur_b.open,
                    admin_g * 1e4,
                    "MEAN_EXIT",
                    tracker.mfe_bps,
                    tracker.mae_bps,
                )
            if boundary_k >= STALL_ELIGIBLE_FROM_BOUNDARY and abs(
                z_frozen
            ) > STALL_EXIT_Z_FRACTION * abs(cand.z_entry):
                tracker.observe(admin_g * 1e4)
                return _S4ExitOutcome(
                    cur_a.open_time,
                    cur_a.open,
                    cur_b.open,
                    admin_g * 1e4,
                    "STALL_EXIT",
                    tracker.mfe_bps,
                    tracker.mae_bps,
                )
            if boundary_k == MAX_HOLD_BARS:
                tracker.observe(admin_g * 1e4)
                return _S4ExitOutcome(
                    cur_a.open_time,
                    cur_a.open,
                    cur_b.open,
                    admin_g * 1e4,
                    "TIMEOUT",
                    tracker.mfe_bps,
                    tracker.mae_bps,
                )
        elif gap_tp:
            tracker.observe(d_tp * 1e4)
            return _S4ExitOutcome(
                cur_a.open_time,
                cur_a.open,
                cur_b.open,
                d_tp * 1e4,
                "TP",
                tracker.mfe_bps,
                tracker.mae_bps,
            )

        g_min_bound, g_max_bound = _conservative_bounds(
            k_a, k_b, cur_a, cur_b, e_a, e_b
        )
        if g_min_bound <= -d_sl:
            tracker.observe(-d_sl * 1e4)
            return _S4ExitOutcome(
                cur_a.open_time,
                cur_a.open,
                cur_b.open,
                -d_sl * 1e4,
                "SL",
                tracker.mfe_bps,
                tracker.mae_bps,
            )
        if g_min_bound >= d_tp:
            tracker.observe(d_tp * 1e4)
            return _S4ExitOutcome(
                cur_a.open_time,
                cur_a.open,
                cur_b.open,
                d_tp * 1e4,
                "TP",
                tracker.mfe_bps,
                tracker.mae_bps,
            )

        tracker.observe(g_min_bound * 1e4)
        tracker.observe(g_max_bound * 1e4)

        next_ts = cur_a.open_time + _MIN_MS
        # R2 fix (verify-R1 finding 1, mirrors rob974_h2_s3_engine): D1's
        # approved fold boundary is INCLUSIVE -- exact
        # `signal_ts + strategy_max_hold == phase_end` must still be
        # readable/evaluable; only reading STRICTLY PAST phase_end is a
        # horizon violation.
        if horizon_end_ts is not None and next_ts > horizon_end_ts:
            return _S4Incomplete("fold_horizon_rejected", entry_ts, e_a, e_b)
        if next_ts >= corpus_end_ts:
            return _S4Incomplete("early_eof", entry_ts, e_a, e_b)
        nxt_a = minute_index.get((symbol_a, next_ts))
        nxt_b = minute_index.get((symbol_b, next_ts))
        if nxt_a is None or nxt_b is None:
            return _S4Incomplete("data_gap_in_pair_position", entry_ts, e_a, e_b)
        cur_a, cur_b = nxt_a, nxt_b


def run_s4_pair_basket_stream(
    candidates: Sequence[S4PairSignalIntent],
    minute_index: MinuteIndex,
    pair_close_index: PairCloseIndex,
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
) -> S4EngineResult:
    trades: list[S4PairTrade] = []
    no_trades: list[S4NoTradeRecord] = []
    incompletes: list[S4IncompleteRecord] = []

    ordered = sorted(candidates, key=lambda c: (c.signal_ts, c.pair))

    # AC2 identity-collision guard: validated UPFRONT, over the WHOLE input
    # (mirrors rob974_h2_s3_engine) -- an incremental in-loop check would be
    # masked whenever an earlier candidate's walk halts the loop (see the
    # INCOMPLETE handling below) before a later duplicate is ever reached.
    seen_identity: set[tuple[tuple[str, str], int]] = set()
    for cand in ordered:
        identity = (cand.pair, cand.signal_ts)
        if identity in seen_identity:
            raise ValueError(
                f"duplicate S4 candidate identity {identity} -- H3 must arbitrate to "
                "at most one candidate per (pair, signal_ts)"
            )
        seen_identity.add(identity)

    position_exit_ts: int | None = None

    for cand in ordered:
        if position_exit_ts is not None and cand.signal_ts < position_exit_ts:
            no_trades.append(_no_trade(cand, "global_position_open"))
            continue

        g_min, g_max = _g_bounds_notional(cand.weight_a, cand.weight_b)
        if g_min > g_max:
            no_trades.append(_no_trade(cand, "g_infeasible"))
            continue
        if abs(cand.gross_notional - g_min) > _G_TOLERANCE:
            no_trades.append(_no_trade(cand, "g_mismatch"))
            continue

        symbol_a, symbol_b = cand.pair
        entry_bar_a = resolve_entry_minute(minute_index, symbol_a, cand.signal_ts)
        entry_bar_b = resolve_entry_minute(minute_index, symbol_b, cand.signal_ts)
        if entry_bar_a is None or entry_bar_b is None:
            no_trades.append(_no_trade(cand, "next_tick_unavailable"))
            continue

        outcome = _walk_s4_position(
            cand,
            entry_bar_a,
            entry_bar_b,
            minute_index,
            pair_close_index,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )

        if isinstance(outcome, _S4Incomplete):
            incompletes.append(
                S4IncompleteRecord(
                    pair=cand.pair,
                    side_a=cand.side_a,
                    side_b=cand.side_b,
                    config_id=cand.config_id,
                    fold_id=cand.fold_id,
                    signal_ts=cand.signal_ts,
                    entry_ts=entry_bar_a.open_time,
                    entry_price_a=outcome.entry_price_a,
                    entry_price_b=outcome.entry_price_b,
                    reason=outcome.reason,
                )
            )
            # Fail closed (mirrors rob974_h2_s3_engine): an unresolvable gap's
            # true close is unknowable, so neither resume-as-flat nor
            # lock-forever is claimed -- stop evaluating further candidates.
            break

        trades.append(
            S4PairTrade(
                pair=cand.pair,
                side_a=cand.side_a,
                side_b=cand.side_b,
                config_id=cand.config_id,
                fold_id=cand.fold_id,
                signal_ts=cand.signal_ts,
                entry_ts=entry_bar_a.open_time,
                weight_a=cand.weight_a,
                weight_b=cand.weight_b,
                beta_a=cand.beta_a,
                beta_b=cand.beta_b,
                mu=cand.mu,
                sigma=cand.sigma,
                z_entry=cand.z_entry,
                gross_notional=cand.gross_notional,
                entry_price_a=entry_bar_a.open,
                entry_price_b=entry_bar_b.open,
                exit_ts=outcome.exit_ts,
                exit_price_a=outcome.exit_price_a,
                exit_price_b=outcome.exit_price_b,
                exit_reason=outcome.exit_reason,
                mfe_bps=outcome.mfe_bps,
                mae_bps=outcome.mae_bps,
                gross_bps=outcome.gross_bps,
                order_id_a=None,
                order_id_b=None,
                pair_exec_status="historical_atomic_assumption",
                pair_executor_validated=False,
                demo_eligible=False,
                volatility_percentile=None,
                volatility_percentile_provenance="not_defined_for_s4",
                pair_exec_fail=PAIR_EXEC_FAIL_NOT_EVALUATED,
                promotion_status=PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
            )
        )
        position_exit_ts = outcome.exit_ts

    return S4EngineResult(
        trades=tuple(trades), no_trades=tuple(no_trades), incompletes=tuple(incompletes)
    )


def _no_trade(cand: S4PairSignalIntent, reason: str) -> S4NoTradeRecord:
    return S4NoTradeRecord(
        pair=cand.pair,
        config_id=cand.config_id,
        fold_id=cand.fold_id,
        signal_ts=cand.signal_ts,
        reason=reason,
    )
