"""Mark and cost inputs for the monitor (hk 7119, ESC 703542 answer + review).

A mark at an instant ``at`` (used for both the KST day-start mark and the
current mark):

* crypto trades 24/7: a stored 1-minute bar that closed at most 10 minutes
  before ``at`` (``fresh_quote``);
* KR / US on a trading day, inside a trading window (KR NXT pre 08:00-08:50,
  KRX regular, NXT/KRX after 15:40-20:00 KST; US 04:00 ET to regular close +
  4h, which covers early closes): a fresh bar at ``at`` (``fresh_quote``);
* in a gap after a window ended that day: the bar that closed within 10 minutes
  of that window's end (``segment_close``);
* before the day's first window or on a non-trading day: the last completed
  session's official close (``last_session_close``), which
  ``latest_close_marks`` returns only when it is the calendar's last completed
  session.

Anything not found is ``None`` and the day stays unmeasured. The US Toss day
market (20:00-04:00 ET) is not a window here: a position is marked at the last
segment or session close then.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daily_loss_monitor.accounts import AccountScope
from app.services.daily_loss_monitor.rules import (
    KR_SELL_TAX_BPS,
    QUOTE_MAX_AGE,
    CostEstimate,
    Mark,
)

MarkFn = Callable[
    [AsyncSession, AccountScope, Sequence[str], datetime],
    Awaitable[dict[str, Mark | None]],
]
#: (session, scope, symbols, until) -> newest bar per symbol that closed at or
#: before ``until`` and no more than QUOTE_MAX_AGE earlier.
BarFn = Callable[
    [AsyncSession, AccountScope, Sequence[str], datetime],
    Awaitable[dict[str, Mark | None]],
]
BoundsFn = Callable[[str, date], "tuple[datetime, datetime] | None"]

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
_POLICY_MARKET = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}
_KR_WINDOWS = ((time(8, 0), time(8, 50)), (time(15, 40), time(20, 0)))


@dataclass(frozen=True)
class FxSnapshot:
    rate: Decimal
    source: str
    observed_at: datetime


def _default_bounds(market: str, day: date):
    from app.services.market_events.session_calendar import regular_session_bounds

    return regular_session_bounds(market, day)  # type: ignore[arg-type]


def trading_windows(
    scope: AccountScope, day: date, bounds_fn: BoundsFn | None = None
) -> list[tuple[datetime, datetime]]:
    """Trading windows of a local exchange day, UTC-aware, sorted; [] if none."""
    bounds = (bounds_fn or _default_bounds)(
        "kr" if scope.instrument_type == "equity_kr" else "us", day
    )
    if bounds is None:
        return []
    if scope.instrument_type == "equity_kr":
        extra = [
            (datetime.combine(day, a, KST), datetime.combine(day, b, KST))
            for a, b in _KR_WINDOWS
        ]
        return sorted([*extra, bounds])
    return [(datetime.combine(day, time(4, 0), ET), bounds[1] + timedelta(hours=4))]


def mark_plan(
    scope: AccountScope, at: datetime, bounds_fn: BoundsFn | None = None
) -> tuple[str, datetime]:
    """``(basis, instant)``: which kind of mark ``at`` needs and its bar cutoff."""
    if scope.instrument_type == "crypto":
        return "fresh_quote", at
    tz = KST if scope.instrument_type == "equity_kr" else ET
    windows = trading_windows(scope, at.astimezone(tz).date(), bounds_fn)
    for start, end in windows:
        if start <= at < end:
            return "fresh_quote", at
    ended = [end for _start, end in windows if end <= at]
    if ended:
        return "segment_close", max(ended)
    return "last_session_close", at


def marks_at(
    bar_fn: BarFn,
    *,
    day_start: bool = False,
    bounds_fn: BoundsFn | None = None,
    close_fn: MarkFn | None = None,
) -> MarkFn:
    """A mark function; with ``day_start`` it marks at the KST day start."""

    async def marks(session, scope, symbols, now):
        from app.services.daily_loss_monitor.monitor import kst_day_start

        at = kst_day_start(now) if day_start else now
        basis, cutoff = mark_plan(scope, at, bounds_fn)
        if basis == "last_session_close":
            found = await (close_fn or last_session_close_marks)(
                session, scope, symbols, at
            )
        else:
            found = await bar_fn(session, scope, symbols, cutoff)
            found = {
                s: None
                if m is None or not (cutoff - QUOTE_MAX_AGE <= m.observed_at <= cutoff)
                else Mark(m.price, m.observed_at, None, basis=basis)
                for s, m in found.items()
            }
        if day_start:
            # The day-start mark is a baseline, not a current quote: rules do
            # not age-check it against now.
            return {
                s: None
                if m is None
                else Mark(m.price, m.observed_at, None, basis=m.basis)
                for s, m in found.items()
            }
        return found

    return marks


async def last_session_close_marks(
    session: AsyncSession, scope: AccountScope, symbols: Sequence[str], at: datetime
) -> dict[str, Mark | None]:
    from app.services.strategy_sleeves.marks import latest_close_marks

    closes = await latest_close_marks(session, scope.instrument_type, list(symbols), at)
    return {
        s: None
        if closes.get(s) is None
        else Mark(closes[s], at, None, basis="last_session_close")
        for s in symbols
    }


def sell_tax_bps(scope: AccountScope) -> Decimal:
    return KR_SELL_TAX_BPS if scope.instrument_type == "equity_kr" else Decimal(0)


def policy_cost_estimate(scope: AccountScope) -> CostEstimate | None:
    """None when the policy cannot be read (the day then stays unmeasured)."""
    try:
        from app.services.trading_policy_service import (
            load_trading_policy,
            policy_version_stamp,
        )

        policy = load_trading_policy().order_proposals.auto_approve
        bps = Decimal(
            str(policy.round_trip_cost_bps[_POLICY_MARKET[scope.instrument_type]])
        )
        stamp = policy_version_stamp()
    except Exception:  # noqa: BLE001 - unreadable policy is an unavailable estimate
        return None
    if not bps.is_finite() or bps < 0:
        return None
    return CostEstimate(
        round_trip_cost_bps=bps,
        sell_tax_bps=sell_tax_bps(scope),
        policy_version=str(stamp.get("version")),
        policy_content_hash=str(stamp.get("content_hash")),
    )
