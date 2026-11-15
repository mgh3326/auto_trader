"""Pure entry-reservation decision for the trend lane.

No DB, network or clock access: the state service reads a fresh
:class:`EntryBook` under the lane advisory lock and asks :func:`decide_entry`.
Every tripped reason is returned (no short-circuit), mirroring ROB-993's
``evaluate_kill_switch``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .constants import LOCKED_LIMITS, TrendLimits

EXIT_HARD_STOP: Final = "hard_stop"

# Signal states that occupy an exposure slot.
ACTIVE_SIGNAL_STATES: Final = ("entry_reserved", "holding", "uncertain")
CONFLICT_SIGNAL_STATES: Final = ("conflict_blocked", "conflict_rejected")


class EntryRefusal:
    SYMBOL_NOT_IN_UNIVERSE = "symbol_not_in_universe"
    INVALID_NOTIONAL = "invalid_notional"
    LEG_NOTIONAL_CAP_EXCEEDED = "leg_notional_cap_exceeded"
    MAX_CONCURRENT_POSITIONS_REACHED = "max_concurrent_positions_reached"
    SYMBOL_ALREADY_ACTIVE = "symbol_already_active"
    UNCERTAIN_INTENT_UNRESOLVED = "uncertain_intent_unresolved"
    GROSS_NOTIONAL_CAP_EXCEEDED = "gross_notional_cap_exceeded"
    CONSECUTIVE_HARD_STOP_LIMIT_REACHED = "consecutive_hard_stop_limit_reached"
    CONFLICT_WEEK_LIMIT_REACHED = "conflict_week_limit_reached"


@dataclass(frozen=True)
class ActiveSlot:
    symbol: str
    notional_usdt: Decimal


@dataclass(frozen=True)
class EntryBook:
    """Durable state read under the lane lock."""

    active: tuple[ActiveSlot, ...]
    uncertain_symbols: frozenset[str]
    #: Longest run of consecutive hard stops anywhere in the current UTC day.
    max_consecutive_hard_stops_today: int
    #: Some UTC week holds ``max_conflicts_per_utc_week`` unresolved conflicts.
    conflict_stop_tripped: bool


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reasons: tuple[str, ...]


def max_consecutive_hard_stops(exit_reasons_oldest_first: Sequence[str | None]) -> int:
    """Longest run of hard stops in the day (the daily stop is a latch).

    Once the threshold run happened, a later exit of another position must not
    re-open entries for the rest of the UTC day, so the whole day is scanned.
    """
    best = run = 0
    for reason in exit_reasons_oldest_first:
        run = run + 1 if reason == EXIT_HARD_STOP else 0
        best = max(best, run)
    return best


def utc_day_start(now: dt.datetime) -> dt.datetime:
    utc = now.astimezone(dt.UTC)
    return dt.datetime.combine(utc.date(), dt.time.min, tzinfo=dt.UTC)


def utc_week_start(now: dt.datetime) -> dt.datetime:
    """Monday 00:00 UTC of the week containing ``now``."""
    day = utc_day_start(now)
    return day - dt.timedelta(days=day.weekday())


def conflict_stop_tripped(
    unresolved_conflict_times: Sequence[dt.datetime], *, limit: int
) -> bool:
    """True when any UTC week holds ``limit`` unresolved conflicts.

    The stop is not cleared by the week boundary: it stays until an operator
    resolves the conflict records (design §4.5).
    """
    per_week: dict[dt.datetime, int] = {}
    for moment in unresolved_conflict_times:
        week = utc_week_start(moment)
        per_week[week] = per_week.get(week, 0) + 1
    return any(count >= limit for count in per_week.values())


def decide_entry(
    *,
    book: EntryBook,
    symbol: str,
    notional_usdt: Decimal,
    limits: TrendLimits = LOCKED_LIMITS,
) -> EntryDecision:
    reasons: list[str] = []
    if symbol not in limits.universe:
        reasons.append(EntryRefusal.SYMBOL_NOT_IN_UNIVERSE)
    valid_notional = notional_usdt.is_finite() and notional_usdt > 0
    if not valid_notional:
        reasons.append(EntryRefusal.INVALID_NOTIONAL)
    elif notional_usdt > limits.leg_notional_cap_usdt:
        reasons.append(EntryRefusal.LEG_NOTIONAL_CAP_EXCEEDED)
    if len(book.active) >= limits.max_concurrent_positions:
        reasons.append(EntryRefusal.MAX_CONCURRENT_POSITIONS_REACHED)
    if any(slot.symbol == symbol for slot in book.active):
        reasons.append(EntryRefusal.SYMBOL_ALREADY_ACTIVE)
    if symbol in book.uncertain_symbols:
        reasons.append(EntryRefusal.UNCERTAIN_INTENT_UNRESOLVED)
    if valid_notional:
        gross = sum((slot.notional_usdt for slot in book.active), Decimal(0))
        if gross + notional_usdt > limits.max_gross_notional_usdt:
            reasons.append(EntryRefusal.GROSS_NOTIONAL_CAP_EXCEEDED)
    if (
        book.max_consecutive_hard_stops_today
        >= limits.max_consecutive_hard_stops_per_utc_day
    ):
        reasons.append(EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED)
    if book.conflict_stop_tripped:
        reasons.append(EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED)
    return EntryDecision(allowed=not reasons, reasons=tuple(reasons))
