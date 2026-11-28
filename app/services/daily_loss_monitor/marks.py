"""Mark and cost inputs for the monitor (hk 7119, ESC 703542 answer).

* Inside the exchange's regular session an open lot is marked at a fresh quote
  (the injected minute-bar reader; rules enforce the 10-minute age).
* Outside it the mark is the last completed session close
  (``basis=last_session_close``); ``latest_close_marks`` returns a close only
  when it is the exchange calendar's last completed session, so a missing or
  older close is ``None`` and the day stays unmeasured.
* Crypto trades 24/7 and always uses the fresh quote.
* Fees not reported by the broker are estimated with the policy round-trip cost
  rate (the same source retro uses), plus the KR sell tax.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daily_loss_monitor.accounts import AccountScope
from app.services.daily_loss_monitor.rules import (
    KR_SELL_TAX_BPS,
    CostEstimate,
    Mark,
)

MarkFn = Callable[
    [AsyncSession, AccountScope, Sequence[str], datetime],
    Awaitable[dict[str, Mark | None]],
]
BoundsFn = Callable[[str, "object"], "tuple[datetime, datetime] | None"]

_SESSION_MARKET = {"equity_kr": "kr", "equity_us": "us"}
_LOCAL_TZ = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}
_POLICY_MARKET = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}


@dataclass(frozen=True)
class FxSnapshot:
    rate: Decimal
    source: str
    observed_at: datetime


def in_regular_session(
    scope: AccountScope, now: datetime, bounds_fn: BoundsFn | None = None
) -> bool:
    market = _SESSION_MARKET.get(scope.instrument_type)
    if market is None:
        return True  # crypto: 24/7, always a fresh quote
    if bounds_fn is None:
        from app.services.market_events.session_calendar import (
            regular_session_bounds as bounds_fn,
        )
    bounds = bounds_fn(market, now.astimezone(_LOCAL_TZ[market]).date())
    return bounds is not None and bounds[0] <= now < bounds[1]


async def last_session_close_marks(
    session: AsyncSession, scope: AccountScope, symbols: Sequence[str], now: datetime
) -> dict[str, Mark | None]:
    from app.services.strategy_sleeves.marks import latest_close_marks

    closes = await latest_close_marks(
        session, scope.instrument_type, list(symbols), now
    )
    return {
        s: None
        if closes.get(s) is None
        else Mark(closes[s], now, None, basis="last_session_close")
        for s in symbols
    }


def session_aware_marks(
    fresh_fn: MarkFn,
    close_fn: MarkFn = last_session_close_marks,
    bounds_fn: BoundsFn | None = None,
) -> MarkFn:
    async def marks(session, scope, symbols, now):
        if in_regular_session(scope, now, bounds_fn):
            found = await fresh_fn(session, scope, symbols, now)
            return {
                s: None if m is None else dataclasses.replace(m, basis="fresh_quote")
                for s, m in found.items()
            }
        return await close_fn(session, scope, symbols, now)

    return marks


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
        sell_tax_bps=KR_SELL_TAX_BPS
        if scope.instrument_type == "equity_kr"
        else Decimal(0),
        policy_version=str(stamp.get("version")),
        policy_content_hash=str(stamp.get("content_hash")),
    )
