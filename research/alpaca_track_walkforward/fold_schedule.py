"""ROB-1062 H4 (Run A SS15, AC1-AC6) — the 8-fold walk-forward schedule.

Each fold is TRAIN(365 days) -> embargo(7 days) -> OOS(28 days), contiguous,
UTC half-open ``[start, end)`` throughout, and the schedule rolls forward by
exactly 28 days (== the OOS length) fold-to-fold, so consecutive folds' OOS
windows are back-to-back and non-overlapping.

``build_fold_schedule`` takes exactly ONE parameter — the first fold's OOS
start timestamp — and returns ALL 8 folds computed purely from it. There is
no caller-reachable way to substitute, add, or re-split an individual fold
(AC6: "fold 교체·추가·재분할은 금지다"): the only way to get a different
fold 3 is to pass a different anchor, which changes every fold, never one
in isolation.

The anchor is required to be UTC-midnight-aligned AND a Monday. Given
``roll_days == oos_days == 28 == 4*7``, adding any whole multiple of 28 days
to a Monday-midnight timestamp always yields another Monday-midnight
timestamp — so this ONE constraint on the anchor is sufficient to guarantee,
for every one of the 8 OOS windows, that AP-A2's weekly Monday-00:05-UTC
decision lands exactly 4 times (AC2) without re-deriving the constraint
per-fold. ``tests/test_fold_schedule.py`` proves this by literal calendar
walk (a real per-minute assertion), never by trusting this arithmetic
argument alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "DAY_MS",
    "EMBARGO_DAYS",
    "OOS_DAYS",
    "OOS_FOLDS",
    "ROLL_DAYS",
    "TRAIN_DAYS",
    "Fold",
    "build_fold_schedule",
]

DAY_MS = 86_400_000

# Run A SS15 — literal, sealed schedule shape. These are schedule-STRUCTURE
# constants (fold count/window lengths), not gate thresholds or cost
# assumptions, so they are NOT read through the H2 seal (H2's `policy`
# identity component independently carries the SAME literals for the
# ROB-846 identity hash — see `seal_consumption.assert_policy_matches_
# schedule_constants`, which fails closed if the two ever diverge).
OOS_FOLDS = 8
TRAIN_DAYS = 365
EMBARGO_DAYS = 7
OOS_DAYS = 28
ROLL_DAYS = 28


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold. Every boundary is UTC-half-open ``[start,
    end)``; ``embargo_start_ms == train_end_ms`` and ``oos_start_ms ==
    embargo_end_ms`` always hold (contiguous, no gap, no overlap)."""

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    embargo_start_ms: int
    embargo_end_ms: int
    oos_start_ms: int
    oos_end_ms: int

    def __post_init__(self) -> None:
        _int(self.fold_index, "fold_index")
        for name in (
            "train_start_ms",
            "train_end_ms",
            "embargo_start_ms",
            "embargo_end_ms",
            "oos_start_ms",
            "oos_end_ms",
        ):
            _int(getattr(self, name), name)
        if self.train_end_ms - self.train_start_ms != TRAIN_DAYS * DAY_MS:
            raise ValueError("train window must be exactly TRAIN_DAYS long")
        if self.embargo_end_ms - self.embargo_start_ms != EMBARGO_DAYS * DAY_MS:
            raise ValueError("embargo window must be exactly EMBARGO_DAYS long")
        if self.oos_end_ms - self.oos_start_ms != OOS_DAYS * DAY_MS:
            raise ValueError("OOS window must be exactly OOS_DAYS long")
        if self.embargo_start_ms != self.train_end_ms:
            raise ValueError("embargo must start exactly where TRAIN ends (no gap)")
        if self.oos_start_ms != self.embargo_end_ms:
            raise ValueError("OOS must start exactly where embargo ends (no gap)")


def _assert_utc_monday_midnight(ts_ms: int, *, label: str) -> None:
    if ts_ms % DAY_MS != 0:
        raise ValueError(f"{label} must be UTC midnight aligned, got {ts_ms}")
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    if dt.weekday() != 0:
        raise ValueError(
            f"{label} must fall on a Monday (weekday()==0), got weekday="
            f"{dt.weekday()} for {ts_ms}"
        )


def build_fold_schedule(anchor_oos_start_ms: int) -> tuple[Fold, ...]:
    """Build all 8 folds from a single anchor: the FIRST fold's OOS start.

    ``anchor_oos_start_ms`` must be UTC-midnight-aligned and a Monday (see
    module docstring for why this one constraint is sufficient for AC2).
    Fails closed (raises) otherwise — never silently rounds/shifts the
    anchor to the nearest valid instant.
    """
    _int(anchor_oos_start_ms, "anchor_oos_start_ms")
    _assert_utc_monday_midnight(anchor_oos_start_ms, label="anchor_oos_start_ms")

    folds: list[Fold] = []
    for i in range(OOS_FOLDS):
        oos_start = anchor_oos_start_ms + i * ROLL_DAYS * DAY_MS
        oos_end = oos_start + OOS_DAYS * DAY_MS
        embargo_end = oos_start
        embargo_start = embargo_end - EMBARGO_DAYS * DAY_MS
        train_end = embargo_start
        train_start = train_end - TRAIN_DAYS * DAY_MS
        folds.append(
            Fold(
                fold_index=i,
                train_start_ms=train_start,
                train_end_ms=train_end,
                embargo_start_ms=embargo_start,
                embargo_end_ms=embargo_end,
                oos_start_ms=oos_start,
                oos_end_ms=oos_end,
            )
        )
    return tuple(folds)
