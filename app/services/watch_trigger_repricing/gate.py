"""ROB-1286 / ROB-1304 — market-specific session gate.

Which calendar, and why not the other one
-----------------------------------------
This gate reuses :mod:`app.services.market_events.session_calendar`
(``exchange_calendars`` XKRX), the same offline calendar the repo's
existing scheduled tasks already gate on. It invents no holiday judgement.

It deliberately does **not** use the ROB-1280 market-calendar router
(``app/routers/market_calendar.py``). That surface is Toss-backed and
returns ``is_open=None`` for three routine, non-holiday reasons --
``toss_api_disabled``, ``toss_calendar_unavailable``,
``date_out_of_calendar_window`` -- so a disabled flag or a flaky vendor
would present as calendar uncertainty on an ordinary trading day. XKRX is
static and offline: ``unknown`` there means the library genuinely could not
classify the date, which is rare and is a real fault rather than a config
state.

The indeterminate decision (§5)
-------------------------------
``unknown`` does **not** run, same as ``closed``. The costs are not
symmetric:

* Running on a day we cannot confirm is open manufactures proposals, and a
  proposal is not inert -- it enters the approval machinery, where the
  §40/51차 auto-approve lane can submit a resting order without a human
  click. Wrongly-timed proposals are therefore not merely wasted work.
* Not running costs latency, not the fire: the event stays unclaimed, so
  B안 (the rep session's end-of-session re-check) still picks it up. The
  fire is delayed, not buried.

The rep-spawn lane elsewhere in the program fails *open* on an
indeterminate calendar because for it the trade is "wasted session vs
missed trading day". This flow creates orders; it does not inherit that
trade-off. Every skip is returned with its reason so an XKRX outage shows
up as a reason on the tick rather than as silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.market_events.session_calendar import (
    regular_session_bounds,
    trading_session_status,
)

__all__ = [
    "KST",
    "SESSION_END_KST",
    "SESSION_START_KST",
    "GateDecision",
    "evaluate_gate",
]

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")

# KR regular session (ROB-1286 설계 1항: 09:00~15:30 KST). End is exclusive.
SESSION_START_KST = time(9, 0)
SESSION_END_KST = time(15, 30)


@dataclass(frozen=True)
class GateDecision:
    """Whether the tick may run, and -- when it may not -- why."""

    should_run: bool
    reason: str
    session_status: str
    kst_date: str
    market_date: str


def evaluate_gate(*, now: datetime, market: str = "kr") -> GateDecision:
    """Decide whether a repricing tick may run at ``now``.

    ``now`` must be timezone-aware; a naive value is a caller bug and is
    rejected rather than assumed to be UTC (guessing the zone here would
    silently shift the whole session window).
    """
    if now.tzinfo is None:
        raise ValueError("evaluate_gate requires a timezone-aware 'now'")

    kst_date = now.astimezone(KST).date().isoformat()
    if market == "crypto":
        # Crypto is 24/7. Applying an equity holiday calendar here would
        # silently strand a valid fire every weekend and US/KR holiday.
        return GateDecision(
            should_run=True,
            reason="ok_24x7",
            session_status="open_24x7",
            kst_date=kst_date,
            market_date=now.astimezone(UTC).date().isoformat(),
        )

    if market not in {"kr", "us"}:
        raise ValueError(f"unsupported watch repricing market {market!r}")

    local = now.astimezone(KST if market == "kr" else ET)
    market_date = local.date().isoformat()
    status = trading_session_status(market, local.date())

    if status == "closed":
        return GateDecision(
            should_run=False,
            reason="market_closed",
            session_status=status,
            kst_date=kst_date,
            market_date=market_date,
        )
    if status != "open":
        # Fail-closed on an unclassifiable date -- see the module docstring.
        return GateDecision(
            should_run=False,
            reason="session_status_indeterminate",
            session_status=status,
            kst_date=kst_date,
            market_date=market_date,
        )

    if market == "us":
        bounds = regular_session_bounds("us", local.date())
        if bounds is None:
            return GateDecision(
                should_run=False,
                reason="session_bounds_indeterminate",
                session_status="unknown",
                kst_date=kst_date,
                market_date=market_date,
            )
        opened_at, closed_at = bounds
        if not opened_at <= now.astimezone(UTC) < closed_at:
            return GateDecision(
                should_run=False,
                reason="outside_regular_session",
                session_status=status,
                kst_date=kst_date,
                market_date=market_date,
            )
        return GateDecision(
            should_run=True,
            reason="ok",
            session_status=status,
            kst_date=kst_date,
            market_date=market_date,
        )

    clock = local.timetz().replace(tzinfo=None)
    if clock < SESSION_START_KST or clock >= SESSION_END_KST:
        return GateDecision(
            should_run=False,
            reason="outside_intraday_window",
            session_status=status,
            kst_date=kst_date,
            market_date=market_date,
        )

    return GateDecision(
        should_run=True,
        reason="ok",
        session_status=status,
        kst_date=kst_date,
        market_date=market_date,
    )
