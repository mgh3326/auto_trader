"""Walk-forward splits on common dates with purge and gap at each boundary.

The boundary ``B`` is the first session of the next period. A training row at
session index ``t`` with label horizon ``h`` is:

* purged when its label window ``t+1 .. t+h`` reaches ``B`` (the last ``h``
  sessions before ``B``);
* gapped when it lies in the ``gap`` sessions directly before the purged block.

So the last kept row is ``B - 1 - purge - gap``. Purge and gap add up, which is
the stricter reading of ``docs/research/experiment-split-leakage-rules.md`` §4.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from research.kr_boosting.config import MIN_GAP, TRAIN_START


class SplitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoundaryCut:
    """What one boundary removed; indexes are positions in the calendar."""

    boundary: date
    last_kept: int  # last usable row before the boundary (-1 if none)
    purged: tuple[date, ...]
    gapped: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class Fold:
    validate_year: int
    train: tuple[date, ...]
    validate: tuple[date, ...]
    cut: BoundaryCut


def cut_before(
    calendar: Sequence[date], boundary: date, *, purge: int, gap: int
) -> BoundaryCut:
    """Sessions removed in front of ``boundary``, the first date of the next period."""

    if list(calendar) != sorted(set(calendar)):
        raise SplitError("calendar must be strictly ascending")
    if purge < 0:
        raise SplitError("purge must be >= 0")
    if gap < MIN_GAP:
        raise SplitError(f"gap must be >= {MIN_GAP} sessions")
    b = bisect.bisect_left(calendar, boundary)
    purge_start = max(b - purge, 0)
    gap_start = max(purge_start - gap, 0)
    return BoundaryCut(
        boundary=boundary,
        last_kept=gap_start - 1,
        purged=tuple(calendar[purge_start:b]),
        gapped=tuple(calendar[gap_start:purge_start]),
    )


def build_fold(
    calendar: Sequence[date],
    validate_year: int,
    *,
    purge: int,
    gap: int,
    train_start: date = TRAIN_START,
) -> Fold:
    """Expanding train up to the cut in front of ``validate_year``."""

    boundary = date(validate_year, 1, 1)
    next_boundary = date(validate_year + 1, 1, 1)
    cut = cut_before(calendar, boundary, purge=purge, gap=gap)
    train = tuple(d for d in calendar[: cut.last_kept + 1] if d >= train_start)
    validate = tuple(d for d in calendar if boundary <= d < next_boundary)
    if not train:
        raise SplitError(f"fold {validate_year} has no training session")
    if validate and train[-1] >= validate[0]:
        raise SplitError("train overlaps validation")
    return Fold(validate_year, train, validate, cut)


def build_folds(
    calendar: Sequence[date],
    years: Sequence[int],
    *,
    purge: int,
    gap: int,
) -> tuple[Fold, ...]:
    return tuple(build_fold(calendar, y, purge=purge, gap=gap) for y in years)


def last_decision_before(
    calendar: Sequence[date], boundary: date, *, purge: int, gap: int
) -> date:
    """Last session a decision may be taken so nothing crosses into ``boundary``."""

    cut = cut_before(calendar, boundary, purge=purge, gap=gap)
    if cut.last_kept < 0:
        raise SplitError("no session before the boundary")
    return calendar[cut.last_kept]
