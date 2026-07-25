"""ROB-945 (H5) -- PBO daily-grid BUILDER seam RED tests.

Fable Q2=A (orch-fable-answer-rob945b-20260718.md) requires the 12-config
PBO grid to come from 12 INDEPENDENT full-window @17 evaluations of the
frozen 4-symbol universe. Per captain correction, the callback returns raw,
independent per-symbol TRADE STREAMS (never a pre-aggregated day map) --
this module performs the actual canonical-sort + UTC-exit-day aggregation
+ zero-fill itself, so the aggregation logic is auditable H5 code, not a
callback's self-attestation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rob940_engine import TradeRecord
from rob945_pbo_builder import (
    ConfigEvaluationResponse,
    Rob945PboBuilderError,
    SymbolOutcome,
    build_pbo_daily_grid,
)
from rob945_pbo_grid import FROZEN_DAY_KEYS

_SYMBOLS = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")
_WINDOW_START = "2025-07-01T00:00:00Z"
_WINDOW_END = "2026-07-01T00:00:00Z"


def _day_to_ts_ms(day_iso: str) -> int:
    return int(
        datetime.combine(
            datetime.fromisoformat(day_iso).date(),
            datetime.min.time(),
            tzinfo=UTC,
        ).timestamp()
        * 1000
    )


def _trade(
    *, strategy, config_id, symbol, day_iso, net_bps, signal_ts=None, entry_ts=None
):
    exit_ts = _day_to_ts_ms(day_iso) + 3_000  # a few seconds into the day
    signal_ts = signal_ts if signal_ts is not None else exit_ts - 2_000
    entry_ts = entry_ts if entry_ts is not None else exit_ts - 1_000
    return TradeRecord(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        side="long",
        signal_ts=signal_ts,
        entry_ts=entry_ts,
        entry_price=100.0,
        exit_ts=exit_ts,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=net_bps + 10.0,
        fee_bps=5.0,
        all_in_bps=10.0,
        funding_bps=0.0,
        net_bps=net_bps,
        fold_id=None,
    )


def _empty_response(
    strategy,
    config_id,
    *,
    cost_bps=17.0,
    window_start=_WINDOW_START,
    window_end=_WINDOW_END,
    scenario="primary_stress",
    symbols=_SYMBOLS,
    trades_by_symbol=None,
    gaps_by_symbol=None,
):
    trades_by_symbol = trades_by_symbol or {}
    gaps_by_symbol = gaps_by_symbol or {}
    return ConfigEvaluationResponse(
        strategy=strategy,
        config_id=config_id,
        scenario_name=scenario,
        cost_bps=cost_bps,
        window_start_iso=window_start,
        window_end_iso=window_end,
        symbol_outcomes=tuple(
            SymbolOutcome(
                symbol=s,
                status="completed",
                trades=tuple(trades_by_symbol.get(s, ())),
                gap_invalid_days=frozenset(gaps_by_symbol.get(s, ())),
            )
            for s in symbols
        ),
    )


def test_exactly_twelve_independent_requests_are_issued_in_canonical_order():
    requests = []

    def _evaluate(request):
        requests.append((request.strategy, request.config_id))
        return _empty_response(request.strategy, request.config_id)

    build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    assert requests == [("S1", f"S1-{i:02d}") for i in range(12)]


def test_request_carries_exact_frozen_scenario_cost_window_and_symbol_order():
    seen = {}

    def _evaluate(request):
        seen["request"] = request
        return _empty_response(request.strategy, request.config_id)

    build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    req = seen["request"]
    assert req.scenario_name == "primary_stress"
    assert req.cost_bps == 17.0
    assert req.window_start_iso == _WINDOW_START
    assert req.window_end_iso == _WINDOW_END
    assert req.symbols == _SYMBOLS


def test_four_symbol_sum_onto_the_same_day():
    day = FROZEN_DAY_KEYS[10]

    def _evaluate(request):
        trades_by_symbol = {
            s: (
                _trade(
                    strategy=request.strategy,
                    config_id=request.config_id,
                    symbol=s,
                    day_iso=day,
                    net_bps=10.0,
                ),
            )
            for s in _SYMBOLS
        }
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol=trades_by_symbol
        )

    grid, _gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    assert grid["S1-00"][day] == pytest.approx(40.0)  # 4 symbols x 10bps


def test_zero_fill_on_no_trade_days():
    def _evaluate(request):
        return _empty_response(request.strategy, request.config_id)

    grid, _gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    assert all(v == 0.0 for v in grid["S1-00"].values())
    assert len(grid["S1-00"]) == 365


def test_exit_day_boundary_is_the_utc_date_of_exit_ts_not_entry_ts():
    day_a, day_b = FROZEN_DAY_KEYS[0], FROZEN_DAY_KEYS[1]
    entry_ts_late_on_day_a = _day_to_ts_ms(day_a) + 86_000_000  # near end of day_a
    exit_ts_early_on_day_b = _day_to_ts_ms(day_b) + 1_000  # just after midnight day_b

    def _evaluate(request):
        trade = TradeRecord(
            strategy=request.strategy,
            config_id=request.config_id,
            symbol="BTCUSDT",
            side="long",
            signal_ts=entry_ts_late_on_day_a - 1_000,
            entry_ts=entry_ts_late_on_day_a,
            entry_price=100.0,
            exit_ts=exit_ts_early_on_day_b,
            exit_price=101.0,
            exit_reason="take_profit",
            gross_bps=20.0,
            fee_bps=5.0,
            all_in_bps=10.0,
            funding_bps=0.0,
            net_bps=15.0,
            fold_id=None,
        )
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol={"BTCUSDT": (trade,)}
        )

    grid, _gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    assert grid["S1-00"][day_a] == 0.0
    assert grid["S1-00"][day_b] == pytest.approx(15.0)


def test_exit_ts_outside_the_frozen_window_fails_closed():
    def _evaluate(request):
        trade = _trade(
            strategy=request.strategy,
            config_id=request.config_id,
            symbol="BTCUSDT",
            day_iso="2027-01-01",
            net_bps=10.0,
        )
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol={"BTCUSDT": (trade,)}
        )

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_wrong_cost_bps_response_fails_closed():
    def _evaluate(request):
        return _empty_response(request.strategy, request.config_id, cost_bps=13.0)

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_missing_symbol_in_response_fails_closed():
    def _evaluate(request):
        return _empty_response(
            request.strategy, request.config_id, symbols=_SYMBOLS[:-1]
        )

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_forged_trade_identity_fails_closed():
    def _evaluate(request):
        trade = _trade(
            strategy=request.strategy,
            config_id="S1-99",
            symbol="BTCUSDT",
            day_iso=FROZEN_DAY_KEYS[0],
            net_bps=10.0,
        )
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol={"BTCUSDT": (trade,)}
        )

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_wrong_strategy_s3_fails_closed():
    def _evaluate(request):
        return _empty_response(request.strategy, request.config_id)

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S3", evaluate_config=_evaluate)


def test_fold_scoped_trade_is_forbidden():
    def _evaluate(request):
        trade = TradeRecord(
            strategy=request.strategy,
            config_id=request.config_id,
            symbol="BTCUSDT",
            side="long",
            signal_ts=_day_to_ts_ms(FROZEN_DAY_KEYS[0]),
            entry_ts=_day_to_ts_ms(FROZEN_DAY_KEYS[0]) + 1_000,
            entry_price=100.0,
            exit_ts=_day_to_ts_ms(FROZEN_DAY_KEYS[0]) + 2_000,
            exit_price=101.0,
            exit_reason="take_profit",
            gross_bps=20.0,
            fee_bps=5.0,
            all_in_bps=10.0,
            funding_bps=0.0,
            net_bps=10.0,
            fold_id="fold-00",  # forbidden -- this is a rolling/selected-OOS ledger marker
        )
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol={"BTCUSDT": (trade,)}
        )

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_non_completed_symbol_status_fails_closed():
    def _evaluate(request):
        return ConfigEvaluationResponse(
            strategy=request.strategy,
            config_id=request.config_id,
            scenario_name="primary_stress",
            cost_bps=17.0,
            window_start_iso=_WINDOW_START,
            window_end_iso=_WINDOW_END,
            symbol_outcomes=tuple(
                SymbolOutcome(
                    symbol=s,
                    status="crashed" if s == "BTCUSDT" else "completed",
                    trades=(),
                    gap_invalid_days=frozenset(),
                )
                for s in _SYMBOLS
            ),
        )

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_shared_response_object_reused_verbatim_across_calls_is_rejected_by_identity_check():
    """A misbehaving callback that returns the EXACT SAME response object
    (not a fresh one) for every request -- a "shared-run"/memoization bug
    -- must be caught: every config after the first will have trades
    claiming a DIFFERENT config_id than what was requested, and the sealed
    identity check rejects it rather than silently reusing S1-00's data."""
    first_response_holder = {}

    def _evaluate(request):
        if "response" not in first_response_holder:
            trade = _trade(
                strategy=request.strategy,
                config_id=request.config_id,
                symbol="BTCUSDT",
                day_iso=FROZEN_DAY_KEYS[0],
                net_bps=10.0,
            )
            first_response_holder["response"] = _empty_response(
                request.strategy,
                request.config_id,
                trades_by_symbol={"BTCUSDT": (trade,)},
            )
        return first_response_holder[
            "response"
        ]  # SAME object every call, no exceptions

    with pytest.raises(Rob945PboBuilderError):
        build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)


def test_shared_response_object_reused_across_calls_does_not_leak_between_configs():
    """A misbehaving callback that returns responses built from ONE shared
    mutable trade list must not let a later call's mutation change an
    earlier config's already-aggregated grid."""
    shared_trades = []

    def _evaluate(request):
        shared_trades.append(
            _trade(
                strategy=request.strategy,
                config_id=request.config_id,
                symbol="BTCUSDT",
                day_iso=FROZEN_DAY_KEYS[0],
                net_bps=10.0,
            )
        )
        # each response only ever contains THIS config's own trade (the
        # correct behavior) -- proving no cross-contamination even though
        # the underlying accumulator list is shared and growing.
        return _empty_response(
            request.strategy,
            request.config_id,
            trades_by_symbol={"BTCUSDT": (shared_trades[-1],)},
        )

    grid, _gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    for i in range(12):
        assert grid[f"S1-{i:02d}"][FROZEN_DAY_KEYS[0]] == pytest.approx(10.0)


def test_linear_revalue_mutant_is_caught_by_distinct_per_config_canaries():
    """Anti-linear-revalue proof: each config's own trade must drive its
    own distinct grid value -- never a scaled copy of another config's."""

    def _evaluate(request):
        index = int(request.config_id.split("-")[1])
        trade = _trade(
            strategy=request.strategy,
            config_id=request.config_id,
            symbol="BTCUSDT",
            day_iso=FROZEN_DAY_KEYS[0],
            net_bps=float(index),
        )
        return _empty_response(
            request.strategy, request.config_id, trades_by_symbol={"BTCUSDT": (trade,)}
        )

    grid, _gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    values = {grid[f"S1-{i:02d}"][FROZEN_DAY_KEYS[0]] for i in range(12)}
    assert values == {float(i) for i in range(12)}


def test_gap_invalid_days_are_unioned_across_the_four_symbols():
    def _evaluate(request):
        gaps_by_symbol = {
            "BTCUSDT": (FROZEN_DAY_KEYS[3],),
            "XRPUSDT": (FROZEN_DAY_KEYS[7],),
        }
        return _empty_response(
            request.strategy, request.config_id, gaps_by_symbol=gaps_by_symbol
        )

    _grid, gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    assert gaps["S1-00"] == frozenset({FROZEN_DAY_KEYS[3], FROZEN_DAY_KEYS[7]})


def test_end_to_end_wires_into_pbo_auxiliary_evidence_and_gap_invalid_still_fails_closed():
    from rob945_pbo_grid import PboGridError, compute_pbo_auxiliary_evidence

    def _evaluate(request):
        return _empty_response(request.strategy, request.config_id)

    grid, gaps = build_pbo_daily_grid(strategy="S1", evaluate_config=_evaluate)
    evidence = compute_pbo_auxiliary_evidence(
        strategy="S1", daily_net_bps_by_config=grid, gap_invalid_days_by_config=gaps
    )
    assert evidence.config_count == 12
    assert evidence.day_count == 365

    def _evaluate_with_gap(request):
        gaps_by_symbol = {"BTCUSDT": (FROZEN_DAY_KEYS[0],)}
        return _empty_response(
            request.strategy, request.config_id, gaps_by_symbol=gaps_by_symbol
        )

    grid2, gaps2 = build_pbo_daily_grid(
        strategy="S1", evaluate_config=_evaluate_with_gap
    )
    with pytest.raises(PboGridError):
        compute_pbo_auxiliary_evidence(
            strategy="S1",
            daily_net_bps_by_config=grid2,
            gap_invalid_days_by_config=gaps2,
        )
