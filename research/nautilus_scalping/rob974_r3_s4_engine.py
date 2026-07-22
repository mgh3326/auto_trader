"""ROB-974 R3 S4 wrapper over the frozen R2 mechanical position walk.

Only the R3-named DTO/result boundary and the account-global orchestration
live here.  Entry resolution, sizing bounds, conservative minute bounds,
exit precedence, STALL/MEAN/TIMEOUT behavior, MFE/MAE tracking, and horizon
semantics are delegated to the frozen R2 authorities without substituting or
clamping the signed observed z.

Pure historical computation: no persistence, network, broker, order, fill,
scheduler, randomness, or current-time access.
"""

from __future__ import annotations

from collections.abc import Sequence

import rob974_h2_s4_engine as frozen_s4
from rob974_h2_dtos import (
    PAIR_EXEC_FAIL_NOT_EVALUATED,
    PROMOTION_BLOCKED_PENDING_PAIR_EXECUTOR,
)
from rob974_h2_ingress import MinuteIndex, resolve_entry_minute
from rob974_r3_s4_dtos import (
    R3S4EngineResult,
    R3S4IncompleteRecord,
    R3S4NoTradeRecord,
    R3S4PairSignalIntent,
    R3S4PairTrade,
)

__all__ = ["run_r3_s4_pair_basket_stream"]

# Deliberate local aliases: the R3 wrapper reuses the frozen implementation,
# while adversarial tests can mutate only this R3 seam and compare it with the
# independently resolved frozen top-level engine.
_frozen_walk_s4_position = frozen_s4._walk_s4_position
_frozen_g_bounds_notional = frozen_s4._g_bounds_notional
_FrozenS4Incomplete = frozen_s4._S4Incomplete


def _position_is_open(signal_ts: int, exit_ts: int | None) -> bool:
    """Half-open global-position window shared with the frozen engine."""

    return exit_ts is not None and signal_ts < exit_ts


def _no_trade(candidate: R3S4PairSignalIntent, reason: str) -> R3S4NoTradeRecord:
    return R3S4NoTradeRecord(
        pair=candidate.pair,
        config_id=candidate.config_id,
        fold_id=candidate.fold_id,
        signal_ts=candidate.signal_ts,
        reason=reason,
    )


def run_r3_s4_pair_basket_stream(
    candidates: Sequence[R3S4PairSignalIntent],
    minute_index: MinuteIndex,
    pair_close_index: frozen_s4.PairCloseIndex,
    *,
    corpus_end_ts: int,
    horizon_end_ts: int | None = None,
) -> R3S4EngineResult:
    """Run the frozen mechanics through the exact registered R3 S4 lineage."""

    if any(type(candidate) is not R3S4PairSignalIntent for candidate in candidates):
        raise TypeError("candidates must contain exact R3S4PairSignalIntent values")

    ordered = sorted(
        candidates, key=lambda candidate: (candidate.signal_ts, candidate.pair)
    )
    seen_identity: set[tuple[tuple[str, str], int]] = set()
    for candidate in ordered:
        identity = (candidate.pair, candidate.signal_ts)
        if identity in seen_identity:
            raise ValueError(
                f"duplicate S4 candidate identity {identity} -- H3 must arbitrate to "
                "at most one candidate per (pair, signal_ts)"
            )
        seen_identity.add(identity)

    trades: list[R3S4PairTrade] = []
    no_trades: list[R3S4NoTradeRecord] = []
    incompletes: list[R3S4IncompleteRecord] = []
    position_exit_ts: int | None = None

    for candidate in ordered:
        if _position_is_open(candidate.signal_ts, position_exit_ts):
            no_trades.append(_no_trade(candidate, "global_position_open"))
            continue

        g_min, g_max = _frozen_g_bounds_notional(candidate.weight_a, candidate.weight_b)
        if g_min > g_max:
            no_trades.append(_no_trade(candidate, "g_infeasible"))
            continue
        if abs(candidate.gross_notional - g_min) > frozen_s4._G_TOLERANCE:
            no_trades.append(_no_trade(candidate, "g_mismatch"))
            continue

        symbol_a, symbol_b = candidate.pair
        entry_bar_a = resolve_entry_minute(minute_index, symbol_a, candidate.signal_ts)
        entry_bar_b = resolve_entry_minute(minute_index, symbol_b, candidate.signal_ts)
        if entry_bar_a is None or entry_bar_b is None:
            no_trades.append(_no_trade(candidate, "next_tick_unavailable"))
            continue

        # R3S4PairSignalIntent is intentionally duck-compatible with the
        # frozen walk. Its z_entry property returns signed observed_z, never
        # the unsigned registered threshold.
        outcome = _frozen_walk_s4_position(  # type: ignore[arg-type]
            candidate,
            entry_bar_a,
            entry_bar_b,
            minute_index,
            pair_close_index,
            corpus_end_ts=corpus_end_ts,
            horizon_end_ts=horizon_end_ts,
        )
        if type(outcome) is _FrozenS4Incomplete:
            incompletes.append(
                R3S4IncompleteRecord(
                    pair=candidate.pair,
                    side_a=candidate.side_a,
                    side_b=candidate.side_b,
                    config_id=candidate.config_id,
                    fold_id=candidate.fold_id,
                    signal_ts=candidate.signal_ts,
                    entry_ts=entry_bar_a.open_time,
                    entry_price_a=outcome.entry_price_a,
                    entry_price_b=outcome.entry_price_b,
                    reason=outcome.reason,
                )
            )
            # The true close is unknown, so account-global state after this
            # point is unknowable. Stop rather than resuming as flat.
            break

        trades.append(
            R3S4PairTrade(
                pair=candidate.pair,
                side_a=candidate.side_a,
                side_b=candidate.side_b,
                config_id=candidate.config_id,
                fold_id=candidate.fold_id,
                signal_ts=candidate.signal_ts,
                entry_ts=entry_bar_a.open_time,
                weight_a=candidate.weight_a,
                weight_b=candidate.weight_b,
                beta_a=candidate.beta_a,
                beta_b=candidate.beta_b,
                mu=candidate.mu,
                sigma=candidate.sigma,
                observed_z=candidate.observed_z,
                z_threshold=candidate.z_threshold,
                gross_notional=candidate.gross_notional,
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

    return R3S4EngineResult(
        trades=tuple(trades),
        no_trades=tuple(no_trades),
        incompletes=tuple(incompletes),
    )
