from __future__ import annotations

from datetime import UTC, datetime

import pytest

import configs as cfg
import dats_engine as eng
import decision_calendar as dc
import indicators as ind
import pit_universe_alpaca as pu
import sizing
from daily_bars import DAY_MS, DailyBar

AP_A1_00 = cfg.build_ap_a1_configs()[0]  # f=14, s=56, m=28, threshold=0.005


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)
WINDOW_END = dc.prior_completed_day_window(DECISION_TS)[1]


def _bars_ending_at_window(closes: list[float]) -> tuple[DailyBar, ...]:
    n = len(closes)
    bars = []
    for i, close in enumerate(closes):
        day_start = WINDOW_END - (n - i) * DAY_MS
        bars.append(
            DailyBar(
                day_start_ms=day_start,
                day_end_ms=day_start + DAY_MS,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=0.0,
                minute_count_observed=1440,
                imputed_minutes=0,
                max_gap_minutes=0,
                gap_in_last_60min=False,
                is_valid=True,
                is_segment_start=(i == 0),
            )
        )
    return tuple(bars)


def _snapshot(eligible: tuple[str, ...]) -> pu.UniverseSnapshot:
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=tuple(sorted(eligible)),
        per_symbol=(),
        n_t=len(eligible),
        meets_min_universe_size=len(eligible) >= 18,
    )


def _uptrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


def _flat_closes(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def test_rejects_a_non_ap_a1_config():
    ap_a2 = cfg.build_ap_a2_configs()[0]
    with pytest.raises(ValueError, match="AP-A1"):
        eng.run_ap_a1_decision(
            decision_ts_ms=DECISION_TS,
            config=ap_a2,
            universe=_snapshot(()),
            bars_by_symbol={},
            prior_state={},
        )


def test_rejects_a_non_decision_timestamp():
    with pytest.raises(ValueError, match="decision"):
        eng.run_ap_a1_decision(
            decision_ts_ms=DECISION_TS + 60_000,
            config=AP_A1_00,
            universe=_snapshot(()),
            bars_by_symbol={},
            prior_state={},
        )


def test_universe_ineligible_flat_symbol_is_rejected_without_computing_indicators():
    # AAA/USD is not in the eligible universe, but IS carried over from a
    # prior decision as a flat position -- it must still be evaluated (and
    # rejected) this decision, unlike a symbol that is neither eligible NOR
    # previously tracked at all (which the engine correctly has nothing to
    # say about).
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(()),  # AAA/USD not eligible
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(_uptrend_closes(90))},
        prior_state={"AAA/USD": eng.AP_A1_PositionState(state="flat")},
    )
    assert len(result.records) == 1
    record = result.records[0]
    assert record.reason_code == "UNIVERSE_INELIGIBLE"
    assert record.action == "NO_ACTION"


def test_insufficient_price_history_when_fewer_than_s_bars_available():
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(_uptrend_closes(10))},
        prior_state={},
    )
    (record,) = result.records
    assert record.reason_code == "INSUFFICIENT_PRICE_HISTORY"


def test_a_strong_uptrend_produces_an_accepted_entry_matching_direct_indicator_calls():
    closes = _uptrend_closes(120, start=100.0, step=0.015)
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={},
    )
    (record,) = result.records
    d = ind.compute_trend_d(closes, f=14, s=56)
    r = ind.compute_momentum_r(closes, m=28)
    sigma20 = ind.annualized_sigma20(closes)
    vol_scale = sizing.compute_vol_scale(sigma20)
    expected_target = sizing.target_notional_ap_a1(vol_scale)
    assert d >= 0.005 and r > 0.0  # sanity: this fixture really is an entry signal
    assert record.reason_code == "ENTRY_ACCEPTED"
    assert record.action == "ENTER"
    assert record.target_notional == pytest.approx(expected_target)
    assert result.new_state["AAA/USD"].state == "long"
    assert result.new_state["AAA/USD"].committed_notional == pytest.approx(
        expected_target
    )


def test_a_flat_price_series_produces_no_entry_signal():
    closes = _flat_closes(120)
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={},
    )
    (record,) = result.records
    assert record.reason_code == "NO_ENTRY_SIGNAL"


def test_an_existing_long_position_exits_on_a_sharp_downtrend():
    closes = [100.0 * (0.97**i) for i in range(120)]  # sharp, sustained decline
    d = ind.compute_trend_d(closes, f=14, s=56)
    r = ind.compute_momentum_r(closes, m=28)
    assert d <= -0.005 or r <= 0.0  # sanity: this really triggers exit

    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={
            "AAA/USD": eng.AP_A1_PositionState(state="long", committed_notional=50.0)
        },
    )
    (record,) = result.records
    assert record.reason_code == "EXIT_TRIGGERED"
    assert record.action == "EXIT"
    assert result.new_state["AAA/USD"].state == "flat"
    assert result.new_state["AAA/USD"].committed_notional == 0.0


def test_an_existing_long_position_holds_through_a_mild_wobble():
    # A gentle, sustained uptrend: D lands INSIDE the hysteresis band while R
    # stays strictly positive -- neither exit condition (D<=-thr OR R<=0)
    # fires, so the long position must simply hold.
    closes = _uptrend_closes(120, step=0.0001)
    d = ind.compute_trend_d(closes, f=14, s=56)
    r = ind.compute_momentum_r(closes, m=28)
    assert -0.005 < d < 0.005  # sanity: fixture really is in the hysteresis band
    assert r > 0.0  # sanity: R alone does not trigger exit either
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD",)),
        bars_by_symbol={"AAA/USD": _bars_ending_at_window(closes)},
        prior_state={
            "AAA/USD": eng.AP_A1_PositionState(state="long", committed_notional=50.0)
        },
    )
    (record,) = result.records
    assert record.reason_code == "HYSTERESIS_HOLD"
    assert record.action == "HOLD"
    assert result.new_state["AAA/USD"].state == "long"
    assert result.new_state["AAA/USD"].committed_notional == 50.0


def test_two_simultaneous_entry_candidates_compete_for_cash_by_d_descending():
    # Two symbols both fire entry, but only enough cash for one $62.50 slot.
    strong = _uptrend_closes(120, step=0.02)  # bigger D
    weak = _uptrend_closes(120, step=0.006)  # smaller (but still qualifying) D

    d_strong = ind.compute_trend_d(strong, f=14, s=56)
    d_weak = ind.compute_trend_d(weak, f=14, s=56)
    assert d_strong > d_weak > 0.0  # sanity on fixture ordering

    # Pre-existing long positions eat almost all of the $2000 equity, leaving
    # room for exactly one ~$62.50 entry.
    prior_state = {
        f"HOLD-{i}/USD": eng.AP_A1_PositionState(state="long", committed_notional=193.0)
        for i in range(10)
    }  # 1930 committed, ~70 left -- exactly one slot, not two

    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("STRONG/USD", "WEAK/USD")),
        bars_by_symbol={
            "STRONG/USD": _bars_ending_at_window(strong),
            "WEAK/USD": _bars_ending_at_window(weak),
        },
        prior_state=prior_state,
    )
    strong_record = next(r for r in result.records if r.symbol == "STRONG/USD")
    weak_record = next(r for r in result.records if r.symbol == "WEAK/USD")
    assert strong_record.reason_code == "ENTRY_ACCEPTED"
    assert weak_record.reason_code == "INSUFFICIENT_CASH"


def test_look_ahead_is_structurally_impossible_appending_future_bars_never_changes_output():
    closes = _uptrend_closes(120, step=0.015)
    base_bars = _bars_ending_at_window(closes)

    # A caller mistake (or a malicious/buggy upstream) appends bars for DAYS
    # AFTER the decision boundary -- AC2 requires the decision's output bytes
    # be completely unaffected.
    future_bars = tuple(
        DailyBar(
            day_start_ms=WINDOW_END + i * DAY_MS,
            day_end_ms=WINDOW_END + (i + 1) * DAY_MS,
            open=1_000_000.0,
            high=1_000_000.0,
            low=1_000_000.0,
            close=1_000_000.0,  # wildly different price -- if consumed, D/R
            # would move dramatically
            volume=0.0,
            minute_count_observed=1440,
            imputed_minutes=0,
            max_gap_minutes=0,
            gap_in_last_60min=False,
            is_valid=True,
            is_segment_start=False,
        )
        for i in range(5)
    )

    universe = _snapshot(("AAA/USD",))
    baseline = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol={"AAA/USD": base_bars},
        prior_state={},
    )
    tampered = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=universe,
        bars_by_symbol={"AAA/USD": base_bars + future_bars},
        prior_state={},
    )
    assert baseline.records == tampered.records
    assert dict(baseline.new_state) == dict(tampered.new_state)


def test_reason_histogram_reconciles_to_the_total_record_count():
    result = eng.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=AP_A1_00,
        universe=_snapshot(("AAA/USD", "BBB/USD")),
        bars_by_symbol={
            "AAA/USD": _bars_ending_at_window(_uptrend_closes(120, step=0.02)),
            "BBB/USD": _bars_ending_at_window(_flat_closes(120)),
        },
        prior_state={},
    )
    assert sum(result.reason_histogram.values()) == len(result.records)
