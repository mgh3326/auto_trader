"""ROB-1062 H4 — trade_ledger: SignalRecord stream -> closed trades."""

from __future__ import annotations

import pytest
import trade_ledger as tl
from daily_bars import SpotMinute
from output_schema import SignalRecord, evidence_hash

_DAY_MS = 86_400_000
_MIN = 60_000
_RAW_T0 = 1_700_000_000_000
_T0 = _RAW_T0 - (_RAW_T0 % _MIN)  # minute-aligned anchor


def _rec(ts, action, reason, notional=0.0):
    return SignalRecord(
        decision_ts_ms=ts,
        strategy="AP-A1",
        config_id="AP-A1-00",
        symbol="BTC/USD",
        action=action,
        target_notional=notional,
        reason_code=reason,
        evidence_hash=evidence_hash({"x": 1}),
    )


def _bars(ts, *, open_price):
    return [
        SpotMinute(
            open_time_ms=ts,
            open=open_price,
            high=open_price + 1,
            low=open_price / 2,
            close=open_price,
            volume=1.0,
        ),
        SpotMinute(
            open_time_ms=ts + _MIN,
            open=open_price,
            high=open_price + 1,
            low=open_price / 2,
            close=open_price,
            volume=1.0,
        ),
    ]


def test_enter_then_exit_produces_one_closed_trade():
    t_entry = _T0
    t_exit = t_entry + 3 * _DAY_MS
    records = [
        _rec(t_entry, "ENTER", "ENTRY_ACCEPTED", notional=62.5),
        _rec(t_entry + _DAY_MS, "HOLD", "TREND_INTACT_HOLD"),
        _rec(t_exit, "EXIT", "EXIT_TRIGGERED"),
    ]
    ref_prices = {t_entry: 100.0, t_exit: 110.0}
    minute_bars = {
        t_entry: _bars(t_entry, open_price=100.2),
        t_exit: _bars(t_exit, open_price=109.9),
    }
    result = tl.build_trades_for_symbol_config(
        records,
        reference_close_by_decision_ts=ref_prices,
        minute_bars_by_decision_ts=minute_bars,
    )
    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.both_legs_filled is True
    assert trade.holding_days == 3
    assert result.open_position is None


def test_enter_with_no_exit_is_reported_as_open_not_closed():
    t_entry = _T0
    records = [_rec(t_entry, "ENTER", "ENTRY_ACCEPTED", notional=62.5)]
    ref_prices = {t_entry: 100.0}
    minute_bars = {t_entry: _bars(t_entry, open_price=100.0)}
    result = tl.build_trades_for_symbol_config(
        records,
        reference_close_by_decision_ts=ref_prices,
        minute_bars_by_decision_ts=minute_bars,
    )
    assert result.closed_trades == ()
    assert result.open_position is not None
    assert result.open_position.entry_decision_ts_ms == t_entry


def test_entry_unfilled_never_opens_a_position():
    t_entry = _T0
    records = [_rec(t_entry, "ENTER", "ENTRY_ACCEPTED", notional=62.5)]
    ref_prices = {t_entry: 100.0}
    minute_bars = {t_entry: _bars(t_entry, open_price=999.0)}  # far above cap
    result = tl.build_trades_for_symbol_config(
        records,
        reference_close_by_decision_ts=ref_prices,
        minute_bars_by_decision_ts=minute_bars,
    )
    assert result.open_position is None
    assert result.closed_trades == ()
    assert result.entry_unfilled_count == 1


def test_exit_unfilled_keeps_position_open():
    t_entry = _T0
    t_exit = t_entry + _DAY_MS
    records = [
        _rec(t_entry, "ENTER", "ENTRY_ACCEPTED", notional=62.5),
        _rec(t_exit, "EXIT", "EXIT_TRIGGERED"),
    ]
    ref_prices = {t_entry: 100.0, t_exit: 100.0}
    minute_bars = {
        t_entry: _bars(t_entry, open_price=100.0),
        t_exit: _bars(t_exit, open_price=1.0),  # far below floor -> unfilled
    }
    result = tl.build_trades_for_symbol_config(
        records,
        reference_close_by_decision_ts=ref_prices,
        minute_bars_by_decision_ts=minute_bars,
    )
    assert result.closed_trades == ()
    assert result.exit_unfilled_count == 1
    assert result.open_position is not None


def test_exit_with_no_open_leg_raises():
    t_exit = _T0
    records = [_rec(t_exit, "EXIT", "EXIT_TRIGGERED")]
    with pytest.raises(ValueError, match="no matching open ENTER"):
        tl.build_trades_for_symbol_config(
            records,
            reference_close_by_decision_ts={t_exit: 1.0},
            minute_bars_by_decision_ts={},
        )


def test_records_must_share_one_symbol_and_config():
    ts = 1_700_000_000_000
    other = SignalRecord(
        decision_ts_ms=ts + _DAY_MS,
        strategy="AP-A1",
        config_id="AP-A1-01",
        symbol="ETH/USD",
        action="NO_ACTION",
        target_notional=0.0,
        reason_code="NO_ENTRY_SIGNAL",
        evidence_hash=evidence_hash({}),
    )
    records = [_rec(ts, "NO_ACTION", "NO_ENTRY_SIGNAL"), other]
    with pytest.raises(ValueError, match="same \\(symbol, config_id\\)"):
        tl.build_trades_for_symbol_config(
            records, reference_close_by_decision_ts={}, minute_bars_by_decision_ts={}
        )


def test_records_must_be_strictly_increasing_by_decision_ts():
    ts = 1_700_000_000_000
    records = [
        _rec(ts, "NO_ACTION", "NO_ENTRY_SIGNAL"),
        _rec(ts, "NO_ACTION", "NO_ENTRY_SIGNAL"),
    ]
    with pytest.raises(ValueError, match="strictly increasing"):
        tl.build_trades_for_symbol_config(
            records, reference_close_by_decision_ts={}, minute_bars_by_decision_ts={}
        )
