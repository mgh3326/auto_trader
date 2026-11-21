"""Slot filling and the late-issue rule (design §4.3, §5.1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .constants import MAX_POSITIONS_PER_ARM
from .signals import Signal
from .universe import Eligibility, liquidity_order

REASON_SELECTED = "selected"
REASON_ALREADY_HELD = "already_held"
REASON_REENTRY_BANNED = "reentry_banned"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_NO_SIGNAL = "no_signal"
REASON_NO_FREE_SLOT = "no_free_slot"

#: An issue later than this after the 09:00 KST candle close is ``late_issue``.
LATE_ISSUE_AFTER = timedelta(minutes=60)


def is_late_issue(*, candle_closed_at: datetime, issued_at: datetime) -> bool:
    if candle_closed_at.tzinfo is None or issued_at.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return issued_at - candle_closed_at > LATE_ISSUE_AFTER


@dataclass(frozen=True)
class EntryDecision:
    market: str
    selected: bool
    reason_code: str


def select_entries(
    *,
    signals: Mapping[str, Signal],
    eligibility: Mapping[str, Eligibility],
    held_markets: Iterable[str],
    banned_markets: Iterable[str],
    open_positions: int,
) -> tuple[EntryDecision, ...]:
    """One arm, one day: who gets an entry intent, with a reason for everyone else.

    A held market never gets a buy intent (no averaging down or adding: it is
    not executable, not penalised). Surplus candidates are ordered by U0
    liquidity (30-day turnover, descending; ties by market code), a
    deterministic tie rule rather than an alpha choice.
    """
    held = set(held_markets)
    banned = set(banned_markets)
    decisions: dict[str, EntryDecision] = {}
    contenders: list[str] = []
    for market in sorted(set(signals) | set(eligibility)):
        signal = signals.get(market)
        elig = eligibility.get(market)
        if elig is None or not elig.eligible:
            decisions[market] = EntryDecision(market, False, REASON_NOT_ELIGIBLE)
        elif signal is None or not signal.enter:
            decisions[market] = EntryDecision(market, False, REASON_NO_SIGNAL)
        elif market in held:
            decisions[market] = EntryDecision(market, False, REASON_ALREADY_HELD)
        elif market in banned:
            decisions[market] = EntryDecision(market, False, REASON_REENTRY_BANNED)
        else:
            contenders.append(market)

    free = max(0, MAX_POSITIONS_PER_ARM - open_positions)
    ranked = [e.market for e in liquidity_order(eligibility[m] for m in contenders)]
    for position, market in enumerate(ranked):
        if position < free:
            decisions[market] = EntryDecision(market, True, REASON_SELECTED)
        else:
            decisions[market] = EntryDecision(market, False, REASON_NO_FREE_SLOT)
    return tuple(decisions[m] for m in sorted(decisions))
