"""Common-date expanding walk-forward; purge and embargo are additive."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .dataset import HORIZONS


@dataclass(frozen=True)
class Fold:
    train_dates: pd.DatetimeIndex
    test_dates: pd.DatetimeIndex


def walk_forward(
    days,
    *,
    first_test: str,
    test_sessions: int = 63,
    min_train: int = 60,
    embargo: int = 10,
    purge: int = 10,
) -> list[Fold]:
    if embargo < max(HORIZONS) or purge < max(HORIZONS):
        raise ValueError("purge and embargo must each cover max horizon (10)")
    if test_sessions < 1 or min_train < 1:
        raise ValueError("positive test_sessions/min_train required")
    days = pd.DatetimeIndex(sorted(set(days)))
    first = int(days.searchsorted(pd.Timestamp(first_test)))
    folds = []
    for start in range(first, len(days), test_sessions):
        stop = start - purge - embargo
        if stop < min_train:
            continue
        folds.append(Fold(days[:stop], days[start : start + test_sessions]))
    if not folds:
        raise ValueError("no fold has sufficient embargoed training history")
    return folds
