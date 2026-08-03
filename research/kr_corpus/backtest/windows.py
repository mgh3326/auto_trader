"""Evaluation / exploration / holdout windows for the KR backtest harness.

Reuses ``research_contracts.evaluation_windows.ClosedWindow`` for the
closed interval representation. Window bounds are the §1 literals.
"""

from __future__ import annotations

from datetime import date

from research_contracts.evaluation_windows import ClosedWindow

__all__ = [
    "CUTOFF_SESSION",
    "EXPLORATION_WINDOW",
    "HOLDOUT_WINDOW",
    "START_DATE",
    "TRAIN_WINDOW",
    "VALIDATION_WINDOW",
    "date_in_closed_window",
    "parse_iso_date",
]

# §1 literals — do not invent alternate bounds.
START_DATE = "2015-01-01"
CUTOFF_SESSION = "2026-07-31"

EXPLORATION_WINDOW = ClosedWindow("2015-01-01", "2024-12-31")
TRAIN_WINDOW = ClosedWindow("2015-01-01", "2022-12-31")
VALIDATION_WINDOW = ClosedWindow("2023-01-01", "2024-12-31")
# HISTORICAL_OOS == HOLDOUT_WINDOW (corpus brief)
HOLDOUT_WINDOW = ClosedWindow("2025-01-01", "2026-07-31")


def parse_iso_date(value: str) -> date:
    if type(value) is not str:
        raise TypeError("date value must be str YYYY-MM-DD")
    return date.fromisoformat(value)


def date_in_closed_window(d: date, window: ClosedWindow) -> bool:
    start = parse_iso_date(window.start)
    end = parse_iso_date(window.end)
    return start <= d <= end
