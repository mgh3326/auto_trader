"""ROB-944 (H4, ROB-940) — canonical signal ordering + duplicate-signal_ts guard.

RED/regression matrix item 8: per-symbol/config duplicate signal_ts
rejection, canonical signal ordering, stable ledger tie-break independent of
input/dict/hash-set iteration order.
"""

from __future__ import annotations

import pytest
from rob940_engine import SignalEvent
from rob944_signal_ordering import (
    DuplicateSignalTimestampError,
    assert_unique_signal_ts_per_symbol_config,
    canonical_signal_sort_key,
    sort_signals_canonically,
)


def _sig(
    strategy="S1", config_id="S1-00", symbol="BTCUSDT", signal_ts=1000, side="long"
):
    return SignalEvent(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side=side,
        sl_distance_bps=50.0,
        tp_distance_bps=100.0,
    )


def test_duplicate_signal_ts_same_strategy_config_symbol_is_rejected():
    signals = [_sig(signal_ts=1000), _sig(signal_ts=1000)]
    with pytest.raises(DuplicateSignalTimestampError):
        assert_unique_signal_ts_per_symbol_config(signals)


def test_same_signal_ts_different_symbol_is_allowed():
    signals = [
        _sig(symbol="BTCUSDT", signal_ts=1000),
        _sig(symbol="XRPUSDT", signal_ts=1000),
    ]
    assert_unique_signal_ts_per_symbol_config(signals)  # must not raise


def test_same_signal_ts_different_config_is_allowed():
    signals = [
        _sig(config_id="S1-00", signal_ts=1000),
        _sig(config_id="S1-01", signal_ts=1000),
    ]
    assert_unique_signal_ts_per_symbol_config(signals)  # must not raise


def test_same_signal_ts_different_strategy_is_allowed():
    signals = [_sig(strategy="S1", signal_ts=1000), _sig(strategy="S2", signal_ts=1000)]
    assert_unique_signal_ts_per_symbol_config(signals)  # must not raise


def test_canonical_sort_key_shape():
    sig = _sig(strategy="S2", config_id="S2-03", symbol="XRPUSDT", signal_ts=42)
    assert canonical_signal_sort_key(sig) == (42, "XRPUSDT", "S2-03", "S2")


def test_sort_signals_canonically_is_independent_of_input_permutation():
    a = _sig(symbol="BTCUSDT", signal_ts=3000)
    b = _sig(symbol="XRPUSDT", signal_ts=1000)
    c = _sig(symbol="DOGEUSDT", signal_ts=2000)
    order1 = sort_signals_canonically([a, b, c])
    order2 = sort_signals_canonically([c, a, b])
    order3 = sort_signals_canonically([b, c, a])
    assert order1 == order2 == order3
    assert [s.signal_ts for s in order1] == [1000, 2000, 3000]


def test_sort_signals_canonically_rejects_duplicates_before_sorting():
    with pytest.raises(DuplicateSignalTimestampError):
        sort_signals_canonically([_sig(signal_ts=1000), _sig(signal_ts=1000)])


def test_sort_signals_canonically_breaks_same_ts_ties_by_symbol_then_config_then_strategy():
    same_ts = [
        _sig(strategy="S2", config_id="S2-00", symbol="XRPUSDT", signal_ts=500),
        _sig(strategy="S1", config_id="S1-01", symbol="BTCUSDT", signal_ts=500),
        _sig(strategy="S1", config_id="S1-00", symbol="BTCUSDT", signal_ts=500),
    ]
    ordered = sort_signals_canonically(same_ts)
    assert [(s.symbol, s.config_id, s.strategy) for s in ordered] == [
        ("BTCUSDT", "S1-00", "S1"),
        ("BTCUSDT", "S1-01", "S1"),
        ("XRPUSDT", "S2-00", "S2"),
    ]
