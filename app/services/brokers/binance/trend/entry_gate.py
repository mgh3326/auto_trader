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
    consecutive_hard_stops_today: int
    conflicts_this_week: int


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reasons: tuple[str, ...]


def consecutive_hard_stops(exit_reasons_newest_first: Sequence[str | None]) -> int:
    """Leading run of hard stops; any other exit breaks the streak."""
    run = 0
    for reason in exit_reasons_newest_first:
        if reason != EXIT_HARD_STOP:
            break
        run += 1
    return run


def utc_day_start(now: dt.datetime) -> dt.datetime:
    utc = now.astimezone(dt.UTC)
    return dt.datetime.combine(utc.date(), dt.time.min, tzinfo=dt.UTC)


def utc_week_start(now: dt.datetime) -> dt.datetime:
    """Monday 00:00 UTC of the week containing ``now``."""
    day = utc_day_start(now)
    return day - dt.timedelta(days=day.weekday())


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
        book.consecutive_hard_stops_today
        >= limits.max_consecutive_hard_stops_per_utc_day
    ):
        reasons.append(EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED)
    if book.conflicts_this_week >= limits.max_conflicts_per_utc_week:
        reasons.append(EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED)
    return EntryDecision(allowed=not reasons, reasons=tuple(reasons))
