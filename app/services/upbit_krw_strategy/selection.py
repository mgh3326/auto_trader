"""Slot filling and the late-issue rule (design §4.3, §5.1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .constants import MAX_POSITIONS_PER_ARM
from .signals import Signal
from .universe import REASON_NO_MARKET_DATA, Eligibility, liquidity_order

REASON_SELECTED = "selected"
REASON_ALREADY_HELD = "already_held"
REASON_REENTRY_BANNED = "reentry_banned"
REASON_NOT_ELIGIBLE = "not_eligible"
#: A held market that is no longer eligible (delisted, warned, unknown warning,
#: dropped from the data). It stays visible; it is never a silent drop.
REASON_HELD_NOT_ELIGIBLE = "held_not_eligible"
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
    """``held`` marks a market the arm holds; ``eligibility_reason`` is then the
    current U-rule verdict (``ok``, ``inactive``, ``market_warning_caution``,
    ``market_warning_unknown``, ``no_market_data`` ...) so the holding state is
    explicit even when the market can no longer be bought."""

    market: str
    selected: bool
    reason_code: str
    held: bool = False
    eligibility_reason: str | None = None


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

    Every held market is represented in the result, also when it has no signal
    and no eligibility row (delisted or dropped from the data): it comes back
    with ``held=True`` and ``held_not_eligible`` rather than disappearing.
    """
    held = set(held_markets)
    banned = set(banned_markets)
    decisions: dict[str, EntryDecision] = {}
    contenders: list[str] = []
    for market in sorted(set(signals) | set(eligibility) | held):
        signal = signals.get(market)
        elig = eligibility.get(market)
        is_held = market in held
        if elig is not None:
            elig_reason: str | None = elig.reason_code
        else:
            elig_reason = REASON_NO_MARKET_DATA if is_held else None

        if elig is None or not elig.eligible:
            reason = REASON_HELD_NOT_ELIGIBLE if is_held else REASON_NOT_ELIGIBLE
            decisions[market] = EntryDecision(
                market, False, reason, is_held, elig_reason
            )
        elif signal is None or not signal.enter:
            decisions[market] = EntryDecision(
                market, False, REASON_NO_SIGNAL, is_held, elig_reason
            )
        elif is_held:
            decisions[market] = EntryDecision(
                market, False, REASON_ALREADY_HELD, is_held, elig_reason
            )
        elif market in banned:
            decisions[market] = EntryDecision(
                market, False, REASON_REENTRY_BANNED, is_held, elig_reason
            )
        else:
            contenders.append(market)

    free = max(0, MAX_POSITIONS_PER_ARM - open_positions)
    ranked = [e.market for e in liquidity_order(eligibility[m] for m in contenders)]
    for position, market in enumerate(ranked):
        reason = eligibility[market].reason_code
        if position < free:
            decisions[market] = EntryDecision(
                market, True, REASON_SELECTED, False, reason
            )
        else:
            decisions[market] = EntryDecision(
                market, False, REASON_NO_FREE_SLOT, False, reason
            )
    return tuple(decisions[m] for m in sorted(decisions))
