"""Acceptance 3: purge + 5-session gap at every boundary, on fixture dates."""

from __future__ import annotations

from datetime import date

import pytest

from research.kr_boosting.config import (
    ADMIT_HORIZON,
    ENTRY_HORIZON,
    HYP_ADMIT,
    HYP_ENTRY,
    SPLITS,
    VALIDATION_YEARS,
)
from research.kr_boosting.splits import (
    SplitError,
    build_fold,
    build_folds,
    cut_before,
    last_decision_before,
)
from tests.research.kr_boosting.helpers import weekdays

CALENDAR = weekdays(date(2015, 1, 1), 2600)


def _index(day: date) -> int:
    return CALENDAR.index(day)


def _first_on_or_after(day: date) -> int:
    return next(i for i, d in enumerate(CALENDAR) if d >= day)


def test_cut_removes_purge_then_gap_directly_before_the_boundary() -> None:
    boundary = date(2019, 1, 1)
    b = _first_on_or_after(boundary)
    cut = cut_before(CALENDAR, boundary, purge=5, gap=5)
    assert cut.purged == tuple(CALENDAR[b - 5 : b])
    assert cut.gapped == tuple(CALENDAR[b - 10 : b - 5])
    assert cut.last_kept == b - 11
    assert len(cut.gapped) == 5
    assert set(cut.purged).isdisjoint(cut.gapped)


def test_kept_rows_have_label_windows_clear_of_the_next_period() -> None:
    for year in VALIDATION_YEARS:
        fold = build_fold(CALENDAR, year, purge=5, gap=5)
        last = _index(fold.train[-1])
        horizon_end = CALENDAR[last + 5]
        assert horizon_end < fold.validate[0]
        # Five more sessions stand between the label's end and the boundary.
        assert _index(fold.validate[0]) - (last + 5) == 6


@pytest.mark.parametrize("hypothesis", [HYP_ADMIT, HYP_ENTRY])
def test_every_fold_of_each_hypothesis_has_purge_and_gap(hypothesis: str) -> None:
    spec = SPLITS[hypothesis]
    folds = build_folds(CALENDAR, VALIDATION_YEARS, purge=spec.purge, gap=spec.gap)
    assert [f.validate_year for f in folds] == list(VALIDATION_YEARS)
    for fold in folds:
        assert len(fold.cut.purged) == spec.purge
        assert len(fold.cut.gapped) == spec.gap
        assert set(fold.train).isdisjoint(fold.cut.purged + fold.cut.gapped)
        distance = _index(fold.validate[0]) - _index(fold.train[-1]) - 1
        assert distance == spec.purge + spec.gap


def test_manifest_horizons_drive_purge_and_gap() -> None:
    assert SPLITS[HYP_ADMIT].purge == ADMIT_HORIZON == 5
    assert SPLITS[HYP_ADMIT].gap == 5
    assert SPLITS[HYP_ENTRY].purge == ENTRY_HORIZON == 25
    assert SPLITS[HYP_ENTRY].gap == 25


def test_training_expands_and_folds_use_common_dates() -> None:
    folds = build_folds(CALENDAR, VALIDATION_YEARS, purge=5, gap=5)
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert earlier.train[0] == later.train[0]
        assert len(later.train) > len(earlier.train)
        assert set(earlier.train) < set(later.train)
    assert folds[0].train[0] == CALENDAR[_first_on_or_after(date(2015, 1, 1))]
    assert all(d.year == 2019 for d in folds[0].validate)


def test_gap_below_five_sessions_is_refused() -> None:
    with pytest.raises(SplitError, match="gap"):
        cut_before(CALENDAR, date(2019, 1, 1), purge=5, gap=4)


def test_unsorted_calendar_is_refused() -> None:
    with pytest.raises(SplitError, match="ascending"):
        cut_before(list(reversed(CALENDAR)), date(2019, 1, 1), purge=5, gap=5)


def test_validation_decisions_stop_before_the_test_boundary() -> None:
    """Calendar ends in 2022: decisions stop so no window reads 2023."""

    pre_test = [d for d in CALENDAR if d.year <= 2022]
    last = last_decision_before(pre_test, date(2023, 1, 1), purge=25, gap=25)
    assert pre_test.index(last) == len(pre_test) - 51
    assert pre_test[pre_test.index(last) + 25] < date(2023, 1, 1)
