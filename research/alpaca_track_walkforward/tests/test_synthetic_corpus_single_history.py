"""ROB-1062 H4 — the corpus is ONE absolute-time history, not a per-fold
generator.

This is the root-cause guard for the v1 fold-replication defect. v1's
``build_bars_by_symbol`` keyed prices off the offset from the caller's
``window_start_ms``, so every fold restarted the same path at index 0. All
eight walk-forward folds then observed a byte-identical price series, one
calendar day carried a different price in each fold, and the 128-cell terminal
artifact collapsed to 16 distinct observations replicated eight times while
reporting ``structural_incomplete == 0``.

No test in the pre-fix suite failed on that. These do.

Count/identity only — no PnL, forward return, or hit-rate surface (CRS-24).
"""

from __future__ import annotations

import itertools

import fold_schedule as fs
import pytest
import run_manifest as rm
import synthetic_fixture as sfx

_FOLDS = fs.build_fold_schedule(rm.CANONICAL_ANCHOR_OOS_START_MS)
_DAY_MS = 86_400_000


def _fold_bars(fold: fs.Fold, *, n_symbols: int = 3) -> dict:
    num_days = (fold.oos_end_ms - fold.train_start_ms) // _DAY_MS
    return sfx.build_bars_by_symbol(
        window_start_ms=fold.train_start_ms,
        num_days=num_days,
        n_symbols=n_symbols,
    )


def test_absolute_day_index_is_epoch_anchored_not_window_relative() -> None:
    assert sfx.absolute_day_index(0) == 0
    assert sfx.absolute_day_index(_DAY_MS) == 1
    # Any instant inside a day maps to that day.
    assert sfx.absolute_day_index(_DAY_MS + 5 * 60_000) == 1
    assert sfx.absolute_day_index(2 * _DAY_MS - 1) == 1
    for fold in _FOLDS:
        assert sfx.absolute_day_index(fold.train_start_ms) == (
            fold.train_start_ms // _DAY_MS
        )
    with pytest.raises(TypeError):
        sfx.absolute_day_index(1.0)


def test_one_calendar_day_has_exactly_one_price_in_every_fold() -> None:
    """The walk-forward premise: all folds slice ONE history.

    Under v1 this failed on the first overlapping day of every fold pair.
    """
    bars_by_fold = {fold.fold_index: _fold_bars(fold) for fold in _FOLDS}
    compared = 0
    for left, right in itertools.combinations(range(len(_FOLDS)), 2):
        for symbol, left_bars in bars_by_fold[left].items():
            right_by_day = {
                bar.day_start_ms: bar for bar in bars_by_fold[right][symbol]
            }
            for bar in left_bars:
                other = right_by_day.get(bar.day_start_ms)
                if other is None:
                    continue
                compared += 1
                assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
                    other.open,
                    other.high,
                    other.low,
                    other.close,
                    other.volume,
                ), (
                    f"{symbol} day {bar.day_start_ms} differs between "
                    f"fold-{left} and fold-{right}"
                )
    # Non-vacuous: consecutive folds overlap by TRAIN_DAYS - ROLL_DAYS days.
    assert compared > 10_000


def test_minute_provider_price_is_a_function_of_the_timestamp_alone() -> None:
    """The minute grid must not be re-phased per fold either.

    v1's provider computed ``(decision_ts_ms - window_start_ms) // DAY_MS``,
    so the same decision instant priced differently in different folds.
    """
    provider = sfx.make_minute_bars_provider(n_symbols=3)
    symbol = sfx.symbol_names(3)[0]
    for fold in _FOLDS:
        # A decision instant that several folds share.
        ts_ms = _FOLDS[-1].train_start_ms + 5 * 60_000
        first = provider(symbol, ts_ms).bars
        second = provider(symbol, ts_ms).bars
        assert [bar.close for bar in first] == [bar.close for bar in second]
        assert first[0].close == sfx.close_for(0, sfx.absolute_day_index(ts_ms))
        assert fold.train_start_ms % _DAY_MS == 0


def test_minute_price_agrees_with_the_daily_bar_for_the_same_day() -> None:
    provider = sfx.make_minute_bars_provider(n_symbols=3)
    fold = _FOLDS[3]
    bars = _fold_bars(fold)
    for symbol_index, symbol in enumerate(sfx.symbol_names(3)):
        by_day = {bar.day_start_ms: bar for bar in bars[symbol]}
        for offset in (0, 17, 200):
            day_start = fold.train_start_ms + offset * _DAY_MS
            evidence = provider(symbol, day_start + 5 * 60_000)
            assert evidence.bars[0].close == by_day[day_start].close
            assert evidence.bars[0].close == sfx.close_for(
                symbol_index, sfx.absolute_day_index(day_start)
            )


def test_every_fold_observes_a_distinct_price_path() -> None:
    """The symptom the operator's condition 2 targets, at corpus level."""
    paths = {
        fold.fold_index: tuple(
            bar.close for bar in _fold_bars(fold)[sfx.symbol_names(3)[0]]
        )
        for fold in _FOLDS
    }
    assert len(set(paths.values())) == len(_FOLDS)

    digests = {
        fold.fold_index: rm.canonical_daily_bars_hash(
            _fold_bars(fold, n_symbols=rm.CANONICAL_SYMBOLS.__len__())
        )
        for fold in _FOLDS
    }
    assert len(set(digests.values())) == len(_FOLDS)
    # And each matches the pinned per-fold identity.
    manifest = rm.canonical_run_manifest()
    for fold_index, digest in digests.items():
        assert manifest.daily_bars_hash_by_fold[f"fold-{fold_index}"] == digest


def test_fold_windows_do_not_overlap_in_oos_and_roll_forward() -> None:
    """Fold boundaries were never the defect; pin them so a future corpus
    change cannot be blamed on the schedule."""
    oos_windows = [(fold.oos_start_ms, fold.oos_end_ms) for fold in _FOLDS]
    for (_, prior_end), (next_start, _) in itertools.pairwise(oos_windows):
        assert prior_end == next_start
    assert len(set(oos_windows)) == len(_FOLDS)
    for fold in _FOLDS:
        assert fold.train_end_ms == fold.embargo_start_ms
        assert fold.embargo_end_ms == fold.oos_start_ms
