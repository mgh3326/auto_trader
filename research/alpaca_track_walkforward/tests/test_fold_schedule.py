"""ROB-1062 H4 (AC1, AC2, AC6) — RED-first: the 8-fold walk-forward
schedule generator does not exist yet. This test module is written and run
BEFORE ``fold_schedule.py`` is created, to capture the failing (RED) state
verbatim before any implementation exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import fold_schedule as fs
import pytest

# A Monday 00:00:00 UTC anchor (first OOS start) — 2024-01-01 is a Monday.
_ANCHOR_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def test_build_fold_schedule_returns_exactly_8_folds():
    folds = fs.build_fold_schedule(_ANCHOR_MS)
    assert len(folds) == 8


def test_every_fold_has_train_365_embargo_7_oos_28_and_half_open_utc_boundaries():
    folds = fs.build_fold_schedule(_ANCHOR_MS)
    day_ms = 86_400_000
    for fold in folds:
        assert fold.oos_end_ms - fold.oos_start_ms == 28 * day_ms
        assert fold.embargo_end_ms - fold.embargo_start_ms == 7 * day_ms
        assert fold.train_end_ms - fold.train_start_ms == 365 * day_ms
        # contiguous, half-open, end exclusive
        assert fold.embargo_start_ms == fold.train_end_ms
        assert fold.oos_start_ms == fold.embargo_end_ms


def test_folds_roll_forward_by_28_days_each():
    folds = fs.build_fold_schedule(_ANCHOR_MS)
    day_ms = 86_400_000
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert later.oos_start_ms - earlier.oos_start_ms == 28 * day_ms


def test_each_oos_window_has_exactly_4_monday_0005_utc_decisions_calendar_assertion():
    """AC2 — a calendar assertion, not a trusted arithmetic identity: walk
    every minute-boundary in each OOS window and count real Monday 00:05:00
    UTC timestamps landing inside it."""
    folds = fs.build_fold_schedule(_ANCHOR_MS)
    for fold in folds:
        mondays_0005 = 0
        t = fold.oos_start_ms
        minute_ms = 60_000
        while t < fold.oos_end_ms:
            dt = datetime.fromtimestamp(t / 1000, tz=UTC)
            if dt.weekday() == 0 and dt.hour == 0 and dt.minute == 5 and dt.second == 0:
                mondays_0005 += 1
            t += minute_ms
        assert mondays_0005 == 4


def test_each_oos_window_has_exactly_28_daily_0005_utc_decisions():
    """AC2 second sentence — AP-A1 daily eval count == OOS day count."""
    folds = fs.build_fold_schedule(_ANCHOR_MS)
    for fold in folds:
        daily_0005 = 0
        t = fold.oos_start_ms
        minute_ms = 60_000
        while t < fold.oos_end_ms:
            dt = datetime.fromtimestamp(t / 1000, tz=UTC)
            if dt.hour == 0 and dt.minute == 5 and dt.second == 0:
                daily_0005 += 1
            t += minute_ms
        assert daily_0005 == 28


def test_anchor_not_utc_midnight_rejected():
    bad_anchor = _ANCHOR_MS + 3_600_000
    import pytest

    with pytest.raises(ValueError, match="UTC midnight"):
        fs.build_fold_schedule(bad_anchor)


def test_anchor_not_a_monday_rejected():
    not_monday = _ANCHOR_MS + 86_400_000  # Tuesday
    import pytest

    with pytest.raises(ValueError, match="Monday"):
        fs.build_fold_schedule(not_monday)


def test_build_fold_schedule_takes_no_fold_list_parameter():
    """AC6 — no caller-reachable way to substitute/add/re-split individual
    folds: the function's only parameter is the anchor timestamp."""
    import inspect

    sig = inspect.signature(fs.build_fold_schedule)
    assert list(sig.parameters) == ["anchor_oos_start_ms"]


def test_report_reproduction_direct_arbitrary_fold_construction_is_rejected():
    real = fs.build_fold_schedule(_ANCHOR_MS)[0]
    with pytest.raises(fs.FoldBindingError, match="direct construction"):
        fs.Fold(
            fold_index=99,
            train_start_ms=real.train_start_ms,
            train_end_ms=real.train_end_ms,
            embargo_start_ms=real.embargo_start_ms,
            embargo_end_ms=real.embargo_end_ms,
            oos_start_ms=real.oos_start_ms,
            oos_end_ms=real.oos_end_ms,
        )


def test_fold_id_must_match_builder_issued_fold_index():
    fold = fs.build_fold_schedule(_ANCHOR_MS)[0]
    fs.assert_registered_fold_binding(fold_id="fold-0", fold=fold)
    with pytest.raises(fs.FoldBindingError, match="does not match"):
        fs.assert_registered_fold_binding(fold_id="fold-7", fold=fold)


def test_second_valid_monday_anchor_cannot_register_another_fold_zero():
    """Verifier reproduction: fold-0 has one process-level run identity."""
    fs.build_fold_schedule(_ANCHOR_MS)
    second_monday_anchor = _ANCHOR_MS + 28 * 86_400_000
    with pytest.raises(fs.FoldBindingError, match="different walk-forward anchor"):
        fs.build_fold_schedule(second_monday_anchor)
