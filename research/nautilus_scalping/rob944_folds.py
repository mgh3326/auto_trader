"""ROB-944 (H4, ROB-940) — rolling walk-forward fold schedule (pure, stdlib).

Generates ROLLING (never expanding) UTC half-open folds per the orch-approved
D3 ruling (``orch-strategy-approval-20260717.md``): train 120d / embargo
exactly 3h as ``[train_end, oos_start)`` / OOS 28d / roll 28d, minimum 6
complete folds.

Every boundary is derived purely from millisecond arithmetic on the caller's
``window_start_ms``/``window_end_ms`` -- no calendar/timezone/DST handling is
needed because all inputs/outputs are already UTC epoch ms and every span
(day/hour) is a fixed millisecond constant. A fold whose OOS end would exceed
``window_end_ms`` is dropped WHOLE (never truncated) -- see
``generate_fold_schedule``.

``generate_fold_schedule`` accepts injectable train/embargo/oos/roll/min-fold
parameters so tests can exercise the walk-forward MECHANICS (leakage,
concatenation, no-reselection) against small synthetic windows without
requiring 120+ real days of fixture bars; the REAL ROB-940 campaign always
calls ``generate_frozen_fold_schedule``, which hardcodes the approved
120d/3h/28d/28d/min-6 contract and takes no schedule parameters of its own.

No DB/network/app/broker/random/current-time imports -- pure stdlib,
deterministic given its input.
"""

from __future__ import annotations

from dataclasses import dataclass

_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 86_400_000

TRAIN_DAYS = 120
EMBARGO_HOURS = 3
OOS_DAYS = 28
ROLL_DAYS = 28
MIN_COMPLETE_FOLDS = 6

TRAIN_MS = TRAIN_DAYS * _MS_PER_DAY
EMBARGO_MS = EMBARGO_HOURS * _MS_PER_HOUR
OOS_MS = OOS_DAYS * _MS_PER_DAY
ROLL_MS = ROLL_DAYS * _MS_PER_DAY


class InsufficientFoldsError(ValueError):
    """Fewer than the required minimum complete folds fit the window."""


@dataclass(frozen=True)
class Fold:
    """One rolling walk-forward fold; every boundary is UTC epoch ms,
    half-open (``*_end_ms`` is exclusive). ``embargo_start_ms`` MUST equal
    ``train_end_ms`` and ``oos_start_ms`` MUST equal ``embargo_end_ms`` --
    the embargo is defined as exactly ``[train_end, oos_start)``, not a
    free-floating span, so any caller building a ``Fold`` by hand (not via
    ``generate_fold_schedule``) still gets that contract enforced.
    """

    fold_id: str
    fold_index: int
    train_start_ms: int
    train_end_ms: int
    embargo_start_ms: int
    embargo_end_ms: int
    oos_start_ms: int
    oos_end_ms: int

    def __post_init__(self) -> None:
        if self.embargo_start_ms != self.train_end_ms:
            raise ValueError(
                f"{self.fold_id}: embargo_start_ms ({self.embargo_start_ms}) must "
                f"equal train_end_ms ({self.train_end_ms}) -- embargo is exactly "
                "[train_end, oos_start)"
            )
        if self.oos_start_ms != self.embargo_end_ms:
            raise ValueError(
                f"{self.fold_id}: oos_start_ms ({self.oos_start_ms}) must equal "
                f"embargo_end_ms ({self.embargo_end_ms}) -- embargo is exactly "
                "[train_end, oos_start)"
            )
        if not (
            self.train_start_ms
            < self.train_end_ms
            < self.embargo_end_ms
            < self.oos_end_ms
        ):
            raise ValueError(
                f"{self.fold_id}: fold boundaries must be strictly increasing "
                f"(train_start={self.train_start_ms}, train_end={self.train_end_ms}, "
                f"embargo_end={self.embargo_end_ms}, oos_end={self.oos_end_ms})"
            )


def generate_fold_schedule(
    window_start_ms: int,
    window_end_ms: int,
    *,
    train_ms: int = TRAIN_MS,
    embargo_ms: int = EMBARGO_MS,
    oos_ms: int = OOS_MS,
    roll_ms: int = ROLL_MS,
    min_complete_folds: int = MIN_COMPLETE_FOLDS,
) -> tuple[Fold, ...]:
    """Generate rolling half-open folds covering ``[window_start_ms, window_end_ms)``.

    Fold ``i``'s train window starts at ``window_start_ms + i * roll_ms``; a
    fold is emitted only if its full ``[train_start, oos_end)`` span fits
    inside the window -- the last, partially-covered fold is dropped WHOLE,
    never truncated to fit. Raises :class:`InsufficientFoldsError` if fewer
    than ``min_complete_folds`` complete folds result, and ``ValueError`` if
    any span parameter is non-positive, the window is empty/inverted, or
    ``roll_ms < oos_ms`` would make consecutive OOS windows overlap.
    """
    if window_end_ms <= window_start_ms:
        raise ValueError(
            f"window_end_ms ({window_end_ms}) must be after window_start_ms "
            f"({window_start_ms})"
        )
    for name, value in (
        ("train_ms", train_ms),
        ("embargo_ms", embargo_ms),
        ("oos_ms", oos_ms),
        ("roll_ms", roll_ms),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {value!r}")
    if roll_ms < oos_ms:
        raise ValueError(
            f"roll_ms ({roll_ms}) < oos_ms ({oos_ms}) would make consecutive "
            "OOS windows overlap -- rejected fail-closed"
        )

    folds: list[Fold] = []
    i = 0
    while True:
        train_start = window_start_ms + i * roll_ms
        train_end = train_start + train_ms
        embargo_end = train_end + embargo_ms
        oos_start = embargo_end
        oos_end = oos_start + oos_ms
        if oos_end > window_end_ms:
            break
        folds.append(
            Fold(
                fold_id=f"fold-{i:02d}",
                fold_index=i,
                train_start_ms=train_start,
                train_end_ms=train_end,
                embargo_start_ms=train_end,
                embargo_end_ms=embargo_end,
                oos_start_ms=oos_start,
                oos_end_ms=oos_end,
            )
        )
        i += 1

    if len(folds) < min_complete_folds:
        raise InsufficientFoldsError(
            f"only {len(folds)} complete fold(s) fit in [{window_start_ms}, "
            f"{window_end_ms}); minimum {min_complete_folds} required"
        )
    return tuple(folds)


def generate_frozen_fold_schedule(
    window_start_ms: int, window_end_ms: int
) -> tuple[Fold, ...]:
    """The exact frozen 120d/3h/28d/28d/min-6 walk-forward schedule (ROB-944).

    Takes no schedule parameters of its own -- the ROB-940 campaign contract
    is fixed; only the (also-frozen) data window is a caller input, so a
    change to the window is visible as a full-campaign hash change rather
    than a silent schedule tweak.
    """
    return generate_fold_schedule(window_start_ms, window_end_ms)
