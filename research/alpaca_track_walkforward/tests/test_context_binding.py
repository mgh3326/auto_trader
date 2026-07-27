"""ROB-1062 H4 (AC4, AC5) — warm-up context provenance binding: an OOS-only
mutation must never change a TRAIN decision's consumed-context hash, and a
mutation INSIDE the consumed window must."""

from __future__ import annotations

import context_binding as cb
from daily_bars import DailyBar

_DAY_MS = 86_400_000


def _bar(day_index, *, close, is_valid=True, is_segment_start=False):
    start = day_index * _DAY_MS
    return DailyBar(
        day_start_ms=start,
        day_end_ms=start + _DAY_MS,
        open=close,
        high=close + 1.0,
        low=close - 1.0 if close > 1.0 else close / 2,
        close=close,
        volume=100.0,
        minute_count_observed=1440,
        imputed_minutes=0,
        max_gap_minutes=0,
        gap_in_last_60min=False,
        is_valid=is_valid,
        is_segment_start=is_segment_start,
    )


def test_context_hash_is_deterministic_for_identical_input():
    bars = {
        "BTC/USD": [_bar(0, close=100.0, is_segment_start=True), _bar(1, close=101.0)]
    }
    a = cb.compute_warmup_context_binding(bars, window_end_ms=2 * _DAY_MS)
    b = cb.compute_warmup_context_binding(bars, window_end_ms=2 * _DAY_MS)
    assert a.combined_context_hash == b.combined_context_hash


def test_mutating_an_oos_only_bar_never_changes_the_train_decisions_context_hash():
    """AC5 — the central guarantee: an OOS-only variant must never move a
    TRAIN decision's consumed-context hash."""
    train_window_end = 2 * _DAY_MS  # TRAIN decision consumes days 0-1 only
    bars_v1 = {
        "BTC/USD": [
            _bar(0, close=100.0, is_segment_start=True),
            _bar(1, close=101.0),
            _bar(2, close=999.0),  # OOS-only day, far beyond train_window_end
        ]
    }
    bars_v2 = {
        "BTC/USD": [
            _bar(0, close=100.0, is_segment_start=True),
            _bar(1, close=101.0),
            _bar(2, close=1.0),  # mutated OOS-only day
        ]
    }
    hash_v1 = cb.compute_warmup_context_binding(
        bars_v1, window_end_ms=train_window_end
    ).combined_context_hash
    hash_v2 = cb.compute_warmup_context_binding(
        bars_v2, window_end_ms=train_window_end
    ).combined_context_hash
    assert hash_v1 == hash_v2


def test_mutating_a_bar_inside_the_consumed_window_does_change_the_hash():
    train_window_end = 2 * _DAY_MS
    bars_v1 = {
        "BTC/USD": [_bar(0, close=100.0, is_segment_start=True), _bar(1, close=101.0)]
    }
    bars_v2 = {
        "BTC/USD": [_bar(0, close=100.0, is_segment_start=True), _bar(1, close=555.0)]
    }
    hash_v1 = cb.compute_warmup_context_binding(
        bars_v1, window_end_ms=train_window_end
    ).combined_context_hash
    hash_v2 = cb.compute_warmup_context_binding(
        bars_v2, window_end_ms=train_window_end
    ).combined_context_hash
    assert hash_v1 != hash_v2


def test_binding_is_immutable():
    binding = cb.compute_warmup_context_binding({}, window_end_ms=_DAY_MS)
    try:
        binding.window_end_ms = 999
        raised = False
    except AttributeError:
        raised = True
    assert raised is True


def test_inner_context_mappings_are_immutable_and_hashes_cover_source_and_features():
    bars = {
        "BTC/USD": [_bar(0, close=100.0, is_segment_start=True), _bar(1, close=101.0)]
    }
    binding = cb.compute_warmup_context_binding(bars, window_end_ms=2 * _DAY_MS)
    try:
        binding.per_symbol_segment_hash["FUTURE/USD"] = "attacker-mutated"
        raised = False
    except TypeError:
        raised = True
    assert raised is True
    assert len(binding.source_corpus_hash) == 64
    assert len(binding.feature_input_hash) == 64
    assert binding.per_symbol_segment_range["BTC/USD"] == (0, 2 * _DAY_MS)
