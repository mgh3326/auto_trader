"""ROB-979 (H2, ROB-974 R2) CP2 -- S3 account-global portfolio engine (pure, stdlib).

ONE account-global position across XRP/DOGE/SOL (ROB-979 AC5): the input
``candidates`` stream is presumed ALREADY arbitrated by H3 into at most one
candidate per instant (AC5/AC11 -- "H3 supplies canonical simultaneous
arbitration; the engine cannot run three independent streams and merge
them"). This engine defensively verifies that arbitration (identity
collision guard below) rather than performing it.

ultrathink decisions (frozen for CP2-CP5; revisit only if orch authority
changes -- see ``/tmp/strategy-worker-rob979-sonnet-checkpoints.md`` CP2 entry):

  * Eager whole-position resolution. Because entry only ever happens at an
    H3-supplied candidate's exact ``signal_ts`` (AC3) and a position's future
    path is a pure function of already-known bars/features, this engine does
    NOT interleave "advance position by one minute, check next candidate,
    advance again". Instead, the moment a position opens it is walked to its
    FULL resolution (exit or terminal-incomplete) immediately, in one pass.
    Subsequent candidates are then gated purely by comparing their
    ``signal_ts`` against that resolved position's ``[entry_ts, exit_ts)``
    window -- this is observably identical to true interleaved walking
    because nothing about a already-open position's future exit can change
    based on a later candidate (H2 never lets a new candidate influence an
    existing position), and it is far simpler to reason about/test.
  * AC11 same-tick arbitration: a candidate is rejected as
    ``global_position_open`` iff ``candidate.signal_ts < position.exit_ts``
    (STRICT). At ``signal_ts == exit_ts`` the existing exit is treated as
    already finalized (half-open ``[entry_ts, exit_ts)`` holding window,
    consistent with the funding/gap-window convention used throughout this
    repo, e.g. ``rob941_funding_sidecar``/``rob944_gap_funding``), so the new
    candidate is evaluated from a genuinely flat account state at that exact
    instant.
  * Cooldown boundary rounding (AC10, "same-symbol cooldown of exactly two
    completed 4h bars"): TIMEOUT/THESIS_EXIT always fill exactly on the 4h
    grid, but a TP/SL fill can land mid-bucket (gap or intrabar touch). The
    "completed 4h bar" the cooldown counts from is the bucket the exit
    landed IN, rounded UP to its closing boundary
    (``_ceil_to_4h_grid(exit_ts)``) — i.e. an SL at minute 3 of a 4h bucket
    still consumes that WHOLE bucket before the 2-bucket cooldown starts.
    The same symbol becomes eligible again only at
    ``_ceil_to_4h_grid(exit_ts) + 2*FOUR_H_MS``.
  * Day gates are GLOBAL, not per-symbol (AC10's wording drops "same-symbol"
    for these two, unlike the cooldown clause; "do not inherit the old
    three-entry/-2R halt" confirms this REPLACES, not extends, ROB-940's
    per-symbol version). "Two entries per entry UTC date" keys off the
    resolved ``entry_ts`` (matching ROB-940's day-bucketing convention,
    reused). "Halt after the second realized SL" keys off the UTC date of
    the SL's ``exit_ts`` -- a hold spanning midnight is attributed to the day
    it actually stopped out, and halts NEW entries for the REST of that date
    (an SL exit realized on date D can still halt entries later that same
    date D, even though the position that produced it may have opened on
    D-1).
  * EOF/gap/horizon 4-way split (AC9 vs AC12), implemented inline in
    ``_walk_s3_position``'s per-minute advance step: reaching a required
    minute STRICTLY PAST ``horizon_end_ts`` (if supplied -- an H4-owned
    walk-forward PIT/phase boundary; D1's approved value: exact equality
    ``signal_ts + strategy_max_hold == phase_end`` is READABLE/evaluable,
    only overrunning it is a violation, so the check is ``next_ts >
    horizon_end_ts``, never ``>=``) is ``fold_horizon_rejected``; reaching
    one AT/after ``corpus_end_ts`` (the caller-declared end of available
    minute data -- a plain data-availability boundary, not a PIT/phase
    boundary, so it deliberately keeps the exclusive ``>=`` convention) is
    ``early_eof``; a minute bar missing from within that available range is
    ``data_gap_in_position``; and a MISSING ``S3CloseFeature`` at an
    otherwise-present boundary minute (price data exists, the completed-4h
    feature snapshot does not) is ``missing_future_data`` -- distinct from a
    raw-bar gap because the upstream feature pipeline, not this engine's own
    minute grid, is what's incomplete.
  * MFE/MAE capping (AC25): every minute strictly BEFORE the exit minute
    contributes its FULL high/low range (both fully "chronologically
    available through exit"). The exit minute itself contributes exactly ONE
    observation: for a gap exit, the bar's own ``open`` (the real, actually
    observed price that triggered the exit -- nothing later in that minute is
    chronologically available, since the position closed at the open); for an
    intrabar touch, the fill price itself (``sl_price``/``tp_price``); for
    THESIS_EXIT/TIMEOUT, the bar's ``open`` (the administrative fill price).
    The opposite-direction extreme of the exit minute is never included --
    its ordering relative to the fill within that same minute is not
    determinable from OHLC alone.
  * An INCOMPLETE outcome (data_gap_in_position/early_eof/
    missing_future_data/fold_horizon_rejected) HALTS all further candidate
    evaluation for the rest of the run. A position's true close after an
    unresolvable gap is unknowable -- silently resuming as though flat would
    fabricate a "closed" state, and silently locking the book forever would
    fabricate an "open forever" state; failing closed by stopping the walk
    entirely claims neither. Callers needing per-segment evidence should
    split the input at a known corpus discontinuity themselves rather than
    rely on this engine to resume past one.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rob940_cost_model import gross_bps
from rob974_h2_dtos import (
    MinuteBar,
    S3CloseFeature,
    S3EngineResult,
    S3IncompleteRecord,
    S3NoTradeRecord,
    S3SignalIntent,
    S3Trade,
)
from rob974_h2_ingress import MinuteIndex, resolve_entry_minute

_MIN_MS = 60_000
FOUR_H_MS = 4 * 3_600_000
MAX_HOLD_BARS = 12  # 12 completed 4h bars == 48h
MAX_HOLD_MS = MAX_HOLD_BARS * FOUR_H_MS
DAILY_MAX_ENTRIES = 2
DAILY_MAX_SL = 2
COOLDOWN_BARS = 2

CloseFeatureIndex = Mapping[tuple[str, int], S3CloseFeature]


def _utc_date(ts_ms: int):
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).date()


def _ceil_to_4h_grid(ts_ms: int) -> int:
    remainder = ts_ms % FOUR_H_MS
    return ts_ms if remainder == 0 else ts_ms + (FOUR_H_MS - remainder)


def _gapped_through_sl(side: str, open_price: float, sl_price: float) -> bool:
    return open_price <= sl_price if side == "long" else open_price >= sl_price


def _gapped_through_tp(side: str, open_price: float, tp_price: float) -> bool:
    return open_price >= tp_price if side == "long" else open_price <= tp_price


def _touched_sl(side: str, bar: MinuteBar, sl_price: float) -> bool:
    return bar.low <= sl_price if side == "long" else bar.high >= sl_price


def _touched_tp(side: str, bar: MinuteBar, tp_price: float) -> bool:
    return bar.high >= tp_price if side == "long" else bar.low <= tp_price


def _thesis_exit(side: str, feat: S3CloseFeature) -> bool:
    if side == "long":
        return feat.m <= 0.0 or feat.close < feat.vwap24
    return feat.m >= 0.0 or feat.close > feat.vwap24


@dataclass
class _ExtremaTracker:
    entry_price: float
    side: str
    mfe_bps: float = 0.0
    mae_bps: float = 0.0

    def observe(self, price: float) -> None:
        value = gross_bps(self.side, self.entry_price, price)
        if value > self.mfe_bps:
            self.mfe_bps = value
        if value < self.mae_bps:
            self.mae_bps = value

    def observe_full_bar(self, bar: MinuteBar) -> None:
        self.observe(bar.high)
        self.observe(bar.low)


@dataclass
class _S3ExitOutcome:
    exit_ts: int
    exit_price: float
    exit_reason: str
    mfe_bps: float
    mae_bps: float


@dataclass
class _S3Incomplete:
    reason: str
    entry_ts: int
    entry_price: float


def _walk_s3_position(
    symbol: str,
    side: str,
    entry_bar: MinuteBar,
    sl_price: float,
    tp_price: float,
    minute_index: MinuteIndex,
    close_feature_index: CloseFeatureIndex,
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None,
) -> _S3ExitOutcome | _S3Incomplete:
    entry_ts = entry_bar.open_time
    deadline_ts = entry_ts + MAX_HOLD_MS
    tracker = _ExtremaTracker(entry_price=entry_bar.open, side=side)
    cur = entry_bar

    while True:
        is_boundary = (
            cur.open_time > entry_ts and (cur.open_time - entry_ts) % FOUR_H_MS == 0
        )

        if _gapped_through_sl(side, cur.open, sl_price):
            tracker.observe(cur.open)
            return _S3ExitOutcome(
                cur.open_time, cur.open, "SL", tracker.mfe_bps, tracker.mae_bps
            )

        gap_tp = _gapped_through_tp(side, cur.open, tp_price)

        if is_boundary:
            if gap_tp:
                tracker.observe(tp_price)
                return _S3ExitOutcome(
                    cur.open_time, tp_price, "TP", tracker.mfe_bps, tracker.mae_bps
                )
            feat = close_feature_index.get((symbol, cur.open_time))
            if feat is None:
                return _S3Incomplete("missing_future_data", entry_ts, entry_bar.open)
            if _thesis_exit(side, feat):
                tracker.observe(cur.open)
                return _S3ExitOutcome(
                    cur.open_time,
                    cur.open,
                    "THESIS_EXIT",
                    tracker.mfe_bps,
                    tracker.mae_bps,
                )
            if cur.open_time == deadline_ts:
                tracker.observe(cur.open)
                return _S3ExitOutcome(
                    cur.open_time, cur.open, "TIMEOUT", tracker.mfe_bps, tracker.mae_bps
                )
        elif gap_tp:
            tracker.observe(tp_price)
            return _S3ExitOutcome(
                cur.open_time, tp_price, "TP", tracker.mfe_bps, tracker.mae_bps
            )

        if _touched_sl(side, cur, sl_price):
            tracker.observe(sl_price)
            return _S3ExitOutcome(
                cur.open_time, sl_price, "SL", tracker.mfe_bps, tracker.mae_bps
            )
        if _touched_tp(side, cur, tp_price):
            tracker.observe(tp_price)
            return _S3ExitOutcome(
                cur.open_time, tp_price, "TP", tracker.mfe_bps, tracker.mae_bps
            )

        tracker.observe_full_bar(cur)

        next_ts = cur.open_time + _MIN_MS
        # R2 fix (verify-R1 finding 1): D1's approved fold boundary is
        # INCLUSIVE -- `signal_ts + strategy_max_hold == phase_end` must be
        # readable/evaluable (so an exact-equality TIMEOUT at the boundary
        # resolves normally); only reading STRICTLY PAST phase_end is a
        # horizon violation. `>=` here previously rejected the exact-equal
        # case a beat too early, before the deadline bar itself was ever
        # read. `corpus_end_ts` (data-availability, not a PIT/phase
        # boundary) intentionally keeps its own `>=` exclusive-end
        # convention below -- D1's inclusive-equality approval applies only
        # to `horizon_end_ts`.
        if horizon_end_ts is not None and next_ts > horizon_end_ts:
            return _S3Incomplete("fold_horizon_rejected", entry_ts, entry_bar.open)
        if next_ts >= corpus_end_ts:
            return _S3Incomplete("early_eof", entry_ts, entry_bar.open)
        nxt = minute_index.get((symbol, next_ts))
        if nxt is None:
            return _S3Incomplete("data_gap_in_position", entry_ts, entry_bar.open)
        cur = nxt


def run_s3_portfolio_stream(
    candidates: Sequence[S3SignalIntent],
    minute_index: MinuteIndex,
    close_feature_index: CloseFeatureIndex,
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
) -> S3EngineResult:
    trades: list[S3Trade] = []
    no_trades: list[S3NoTradeRecord] = []
    incompletes: list[S3IncompleteRecord] = []

    ordered = sorted(candidates, key=lambda c: (c.signal_ts, c.symbol))

    # AC2 identity-collision guard: validated UPFRONT, over the WHOLE input,
    # before any walking begins -- not incrementally inside the loop below.
    # An incremental check would be masked whenever an earlier candidate's
    # walk halts the loop (see the INCOMPLETE handling below) before a later
    # duplicate is ever reached, silently hiding an H3 arbitration failure.
    seen_identity: set[tuple[str, int]] = set()
    for cand in ordered:
        identity = (cand.symbol, cand.signal_ts)
        if identity in seen_identity:
            raise ValueError(
                f"duplicate S3 candidate identity {identity} -- H3 must arbitrate to "
                "at most one candidate per (symbol, signal_ts)"
            )
        seen_identity.add(identity)

    position_exit_ts: int | None = (
        None  # exclusive upper bound of the last-opened position
    )
    cooldown_until: dict[str, int] = {}
    entries_by_date: dict = {}
    sl_count_by_date: dict = {}
    sl_halted_dates: set = set()

    for cand in ordered:
        if position_exit_ts is not None and cand.signal_ts < position_exit_ts:
            no_trades.append(_no_trade(cand, "global_position_open"))
            continue

        if (
            cand.symbol in cooldown_until
            and cand.signal_ts < cooldown_until[cand.symbol]
        ):
            no_trades.append(_no_trade(cand, "cooldown_active"))
            continue

        entry_bar = resolve_entry_minute(minute_index, cand.symbol, cand.signal_ts)
        if entry_bar is None:
            no_trades.append(_no_trade(cand, "next_tick_unavailable"))
            continue

        entry_date = _utc_date(entry_bar.open_time)
        if entry_date in sl_halted_dates:
            no_trades.append(_no_trade(cand, "sl_halt_active"))
            continue
        if entries_by_date.get(entry_date, 0) >= DAILY_MAX_ENTRIES:
            no_trades.append(_no_trade(cand, "daily_entry_cap"))
            continue

        sl_price = (
            entry_bar.open * (1.0 - cand.entry_sl_distance)
            if cand.side == "long"
            else entry_bar.open * (1.0 + cand.entry_sl_distance)
        )
        tp_price = (
            entry_bar.open * (1.0 + cand.entry_tp_distance)
            if cand.side == "long"
            else entry_bar.open * (1.0 - cand.entry_tp_distance)
        )

        outcome = _walk_s3_position(
            cand.symbol,
            cand.side,
            entry_bar,
            sl_price,
            tp_price,
            minute_index,
            close_feature_index,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )

        entries_by_date[entry_date] = entries_by_date.get(entry_date, 0) + 1

        if isinstance(outcome, _S3Incomplete):
            incompletes.append(
                S3IncompleteRecord(
                    symbol=cand.symbol,
                    side=cand.side,
                    config_id=cand.config_id,
                    fold_id=cand.fold_id,
                    signal_ts=cand.signal_ts,
                    entry_ts=entry_bar.open_time,
                    entry_price=outcome.entry_price,
                    reason=outcome.reason,
                )
            )
            # A genuinely unresolved position's true close is unknowable -- do
            # not resume as if flat (that would silently fabricate a "closed"
            # state) and do not fabricate an infinite lock either. Fail
            # closed: stop evaluating further candidates for the rest of this
            # run: any later candidate's fate would rest on undefined ground.
            break

        trades.append(
            S3Trade(
                symbol=cand.symbol,
                side=cand.side,
                config_id=cand.config_id,
                fold_id=cand.fold_id,
                signal_ts=cand.signal_ts,
                entry_ts=entry_bar.open_time,
                entry_price=entry_bar.open,
                exit_ts=outcome.exit_ts,
                exit_price=outcome.exit_price,
                exit_reason=outcome.exit_reason,
                mfe_bps=outcome.mfe_bps,
                mae_bps=outcome.mae_bps,
                gross_bps=gross_bps(cand.side, entry_bar.open, outcome.exit_price),
                volatility_percentile=cand.volatility_percentile,
            )
        )
        position_exit_ts = outcome.exit_ts
        exit_boundary = _ceil_to_4h_grid(outcome.exit_ts)
        cooldown_until[cand.symbol] = exit_boundary + COOLDOWN_BARS * FOUR_H_MS

        if outcome.exit_reason == "SL":
            exit_date = _utc_date(outcome.exit_ts)
            sl_count_by_date[exit_date] = sl_count_by_date.get(exit_date, 0) + 1
            if sl_count_by_date[exit_date] >= DAILY_MAX_SL:
                sl_halted_dates.add(exit_date)

    return S3EngineResult(
        trades=tuple(trades), no_trades=tuple(no_trades), incompletes=tuple(incompletes)
    )


def _no_trade(cand: S3SignalIntent, reason: str) -> S3NoTradeRecord:
    return S3NoTradeRecord(
        symbol=cand.symbol,
        side=cand.side,
        config_id=cand.config_id,
        fold_id=cand.fold_id,
        signal_ts=cand.signal_ts,
        reason=reason,
    )
