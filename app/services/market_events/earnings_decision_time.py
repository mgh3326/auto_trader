"""Lookahead-safe date-only -> decision-session labeling for earnings (ROB-371).

Finnhub equity earnings are 100% date-only (``release_time_utc`` is always NULL;
only a BMO/AMC ``time_hint`` is available). To study event reactions without
lookahead bias we map each event to the first daily bar that could legitimately
trade on the news, at **daily granularity only** — intraday labeling is
forbidden (ROB-367 hard boundary).

Anchors:
* ``next_open``            — BMO: react on the event session's OPEN. News is
                             public before the open, so the event session itself
                             (or the next session, if the event date is closed)
                             is the first lookahead-safe bar.
* ``next_close``           — AMC: react on the NEXT session's CLOSE (not its
                             open). After-close news means the event session has
                             already closed before the news broke.
* ``whole_day_uncertain``  — intraday/unknown timing; treat the next clean full
                             session as the reaction window (no open/close pin).
                             ``unknown`` is conservatively treated as worst-case
                             AMC (the event session's close may predate the news)
                             so it maps to the NEXT session, never ``event_date``.
* ``unmappable``           — calendar could not confirm a session within bounds
                             (fail-closed); ``decision_session`` is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.services.market_events.session_calendar import (
    Market,
    is_trading_session,
    next_trading_session,
)

Anchor = Literal["next_open", "next_close", "whole_day_uncertain", "unmappable"]

# Finnhub time_hint values (taxonomy.TIME_HINTS).
_BMO = "before_open"
_AMC = "after_close"
_INTRADAY = "during_market"


@dataclass(frozen=True)
class EarningsDecisionLabel:
    event_date: date
    time_hint: str | None
    decision_session: date | None
    anchor: Anchor
    is_lookahead_safe: bool
    is_intraday_rejected: bool


def _event_or_next_session(market: Market, event_date: date) -> date | None:
    if is_trading_session(market, event_date):
        return event_date
    return next_trading_session(market, event_date)


def label_earnings_decision_time(
    event_date: date,
    time_hint: str | None,
    market: Market = "us",
) -> EarningsDecisionLabel:
    """Map a date-only earnings event to a lookahead-safe decision session."""

    def _unmappable() -> EarningsDecisionLabel:
        return EarningsDecisionLabel(
            event_date=event_date,
            time_hint=time_hint,
            decision_session=None,
            anchor="unmappable",
            is_lookahead_safe=False,
            is_intraday_rejected=False,
        )

    if time_hint == _BMO:
        session = _event_or_next_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "next_open", True, False
        )

    if time_hint == _AMC:
        session = next_trading_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "next_close", True, False
        )

    if time_hint == _INTRADAY:
        # Intraday timing cannot be pinned to a lookahead-safe daily bar; use the
        # next clean full session as the reaction window.
        session = next_trading_session(market, event_date)
        if session is None:
            return _unmappable()
        return EarningsDecisionLabel(
            event_date, time_hint, session, "whole_day_uncertain", True, True
        )

    # unknown / None: could have been AMC, so the event session's close may be
    # history before the news. Conservatively map to the NEXT session (worst-case
    # AMC), never event_date. (ROB-371 lookahead resolution B1.)
    session = next_trading_session(market, event_date)
    if session is None:
        return _unmappable()
    return EarningsDecisionLabel(
        event_date, time_hint, session, "whole_day_uncertain", True, False
    )
