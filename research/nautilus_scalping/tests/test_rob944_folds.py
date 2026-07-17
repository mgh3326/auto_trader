"""ROB-944 (H4, ROB-940) — rolling walk-forward fold schedule tests.

Covers the RED/regression matrix item 1: exact one-year fold boundaries/count,
120d/3h/28d/28d half-open schedule, last-incomplete-fold exclusion (never
truncated), and minimum-six refusal.
"""

from __future__ import annotations

import pytest
import rob941_frozen_scope as frozen
from rob944_folds import (
    EMBARGO_MS,
    MIN_COMPLETE_FOLDS,
    OOS_MS,
    ROLL_MS,
    TRAIN_MS,
    Fold,
    InsufficientFoldsError,
    generate_fold_schedule,
    generate_frozen_fold_schedule,
)

_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000


def test_frozen_constants_match_the_approved_contract():
    assert TRAIN_MS == 120 * _DAY_MS
    assert EMBARGO_MS == 3 * _HOUR_MS
    assert OOS_MS == 28 * _DAY_MS
    assert ROLL_MS == 28 * _DAY_MS
    assert MIN_COMPLETE_FOLDS == 6


def test_frozen_one_year_window_yields_exactly_eight_complete_folds():
    folds = generate_frozen_fold_schedule(frozen.WINDOW_START_MS, frozen.WINDOW_END_MS)
    assert len(folds) == 8


def test_frozen_window_fold_boundaries_pinned_exactly():
    folds = generate_frozen_fold_schedule(frozen.WINDOW_START_MS, frozen.WINDOW_END_MS)
    expected = [
        (0, 1751328000000, 1761696000000, 1761706800000, 1764126000000),
        (1, 1753747200000, 1764115200000, 1764126000000, 1766545200000),
        (2, 1756166400000, 1766534400000, 1766545200000, 1768964400000),
        (3, 1758585600000, 1768953600000, 1768964400000, 1771383600000),
        (4, 1761004800000, 1771372800000, 1771383600000, 1773802800000),
        (5, 1763424000000, 1773792000000, 1773802800000, 1776222000000),
        (6, 1765843200000, 1776211200000, 1776222000000, 1778641200000),
        (7, 1768262400000, 1778630400000, 1778641200000, 1781060400000),
    ]
    for fold, (idx, train_start, train_end, oos_start, oos_end) in zip(
        folds, expected, strict=True
    ):
        assert fold.fold_index == idx
        assert fold.train_start_ms == train_start
        assert fold.train_end_ms == train_end
        assert fold.embargo_start_ms == train_end
        assert fold.embargo_end_ms == oos_start
        assert fold.oos_start_ms == oos_start
        assert fold.oos_end_ms == oos_end


def test_embargo_is_exactly_three_hours_half_open_train_end_to_oos_start():
    folds = generate_frozen_fold_schedule(frozen.WINDOW_START_MS, frozen.WINDOW_END_MS)
    for fold in folds:
        assert fold.embargo_end_ms - fold.embargo_start_ms == 3 * _HOUR_MS
        assert fold.embargo_start_ms == fold.train_end_ms
        assert fold.embargo_end_ms == fold.oos_start_ms


def test_last_incomplete_fold_is_excluded_never_truncated():
    # The would-be 9th fold's own oos_end (train_start rolled by 8*ROLL_MS,
    # then a full train+embargo+oos span) sits a fixed distance past the
    # frozen window's end -- independently derived here, not assumed to be
    # exactly one ROLL_MS. Extending the window by one ms LESS than that
    # distance must still exclude the 9th fold WHOLE (never truncated);
    # extending by exactly that distance must admit it fully.
    ninth_fold_train_start = frozen.WINDOW_START_MS + 8 * ROLL_MS
    ninth_fold_oos_end = ninth_fold_train_start + TRAIN_MS + EMBARGO_MS + OOS_MS
    margin_needed = ninth_fold_oos_end - frozen.WINDOW_END_MS
    assert margin_needed > 0

    folds = generate_fold_schedule(
        frozen.WINDOW_START_MS,
        frozen.WINDOW_END_MS + margin_needed - 1,
    )
    assert len(folds) == 8  # NOT a 9th fold with a truncated oos_end
    for fold in folds:
        assert fold.oos_end_ms <= frozen.WINDOW_END_MS + margin_needed - 1

    folds_with_ninth = generate_fold_schedule(
        frozen.WINDOW_START_MS, frozen.WINDOW_END_MS + margin_needed
    )
    assert len(folds_with_ninth) == 9
    assert folds_with_ninth[8].oos_end_ms == ninth_fold_oos_end


def test_fewer_than_six_complete_folds_is_rejected():
    # A window barely wide enough for exactly 6 folds minus one roll period.
    six_fold_span = TRAIN_MS + EMBARGO_MS + OOS_MS + 5 * ROLL_MS
    with pytest.raises(InsufficientFoldsError):
        generate_fold_schedule(0, six_fold_span - 1)


def test_exactly_six_complete_folds_is_accepted():
    six_fold_span = TRAIN_MS + EMBARGO_MS + OOS_MS + 5 * ROLL_MS
    folds = generate_fold_schedule(0, six_fold_span)
    assert len(folds) == 6


def test_no_two_folds_oos_windows_overlap():
    folds = generate_frozen_fold_schedule(frozen.WINDOW_START_MS, frozen.WINDOW_END_MS)
    for prev, cur in zip(folds, folds[1:], strict=False):
        assert prev.oos_end_ms <= cur.oos_start_ms


def test_overlapping_oos_windows_from_a_too_short_roll_are_rejected():
    # roll_ms < oos_ms would make consecutive OOS windows overlap -- must
    # fail closed rather than silently emit overlapping folds.
    with pytest.raises(ValueError):
        generate_fold_schedule(
            0,
            TRAIN_MS + EMBARGO_MS + OOS_MS + 6 * (OOS_MS // 2),
            roll_ms=OOS_MS // 2,
        )


def test_fold_construction_rejects_non_monotonic_boundaries():
    with pytest.raises(ValueError):
        Fold(
            fold_id="bad",
            fold_index=0,
            train_start_ms=100,
            train_end_ms=50,  # train_end before train_start
            embargo_start_ms=50,
            embargo_end_ms=60,
            oos_start_ms=60,
            oos_end_ms=70,
        )


def test_fold_construction_rejects_embargo_start_not_matching_train_end():
    with pytest.raises(ValueError):
        Fold(
            fold_id="bad",
            fold_index=0,
            train_start_ms=0,
            train_end_ms=100,
            embargo_start_ms=101,  # must equal train_end_ms
            embargo_end_ms=200,
            oos_start_ms=200,
            oos_end_ms=300,
        )


def test_fold_construction_rejects_oos_start_not_matching_embargo_end():
    with pytest.raises(ValueError):
        Fold(
            fold_id="bad",
            fold_index=0,
            train_start_ms=0,
            train_end_ms=100,
            embargo_start_ms=100,
            embargo_end_ms=200,
            oos_start_ms=201,  # must equal embargo_end_ms
            oos_end_ms=300,
        )


def test_non_positive_span_parameters_are_rejected():
    with pytest.raises(ValueError):
        generate_fold_schedule(0, 10**12, train_ms=0)
    with pytest.raises(ValueError):
        generate_fold_schedule(0, 10**12, embargo_ms=-1)
    with pytest.raises(ValueError):
        generate_fold_schedule(0, 10**12, oos_ms=0)
    with pytest.raises(ValueError):
        generate_fold_schedule(0, 10**12, roll_ms=0)


def test_window_end_not_after_window_start_is_rejected():
    with pytest.raises(ValueError):
        generate_fold_schedule(100, 100)
    with pytest.raises(ValueError):
        generate_fold_schedule(100, 50)
