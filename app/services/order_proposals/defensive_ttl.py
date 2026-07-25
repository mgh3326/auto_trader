"""Defensive-proposal approval-window TTL floor (ROB-929).

07-15 US 방어 제안 6건(GOOGL/XOM/AMZN/CRM 트림 + ORCL/AEM 매수) 전건이 미응답 만료됨 --
approval_nonce가 실제 Telegram 승인 창 밖에서 만료돼 조용히 죽었다. This module is the
single source of the observed approval-window constants and the pure floor
computation; ``OrderProposalsService.create_proposal`` applies it only to
``exit_intent in DEFENSIVE_EXIT_INTENTS`` proposals, and only as a *floor* --
a caller-supplied ``valid_until`` that already exceeds the window end is left
untouched.

stdlib only, no DB/network -- mirrors ``state_machine.py``'s dependency-free
design so this stays trivially unit-testable.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.core.timezone import KST

DEFENSIVE_EXIT_INTENTS: frozenset[str] = frozenset({"loss_cut", "defensive_trim"})

# Observed Telegram approval activity windows (KST). US: 22:30-23:30. KR: the
# 08:10-09:30 morning check-in plus a shorter noon look-in -- the noon window's
# exact width isn't in the 07-15 research record, so 12:00-12:15 is a
# conservative operator-adjustable default (see ROB-929 PR notes).
_US_APPROVAL_WINDOWS: tuple[tuple[time, time], ...] = ((time(22, 30), time(23, 30)),)
_KR_APPROVAL_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(8, 10), time(9, 30)),
    (time(12, 0), time(12, 15)),
)

_APPROVAL_WINDOWS_BY_MARKET: dict[str, tuple[tuple[time, time], ...]] = {
    "equity_us": _US_APPROVAL_WINDOWS,
    "equity_kr": _KR_APPROVAL_WINDOWS,
}


def resolve_defensive_valid_until(market: str, now: datetime) -> datetime | None:
    """Return the end of the next KR/US approval window at/after ``now``.

    ``None`` when ``market`` has no defined approval window (e.g. crypto) --
    callers must leave ``valid_until`` unmodified in that case.
    """
    windows = _APPROVAL_WINDOWS_BY_MARKET.get(market)
    if not windows:
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    local_now = now.astimezone(KST)
    today = local_now.date()
    todays_ends = [datetime.combine(today, end, tzinfo=KST) for _, end in windows]
    upcoming = [end_dt for end_dt in todays_ends if end_dt > local_now]
    if upcoming:
        return min(upcoming)

    first_end = min(end for _, end in windows)
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, first_end, tzinfo=KST)
