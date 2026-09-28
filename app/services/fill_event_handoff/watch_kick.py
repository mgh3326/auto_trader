"""Watch-event kick classification and read source (task #865).

Mirror of the #825 fill kick filter for delivered ``investment_watch_events``
rows.  The lane-event bundle path (``bundle.py``) stays unchanged — this module
only decides whether a delivered watch event may consume a shared kick slot.

Kick-eligible requires ALL of:

- ``action_mode == "approval_required"``
- ``intent == "buy_review"`` OR the source alert's ``max_action.side`` present
- the market inside its tradable session at evaluation time

Everything else is queue-only.  ``notify_only`` events can never kick.  Facts
that cannot be read (missing alert row, absent ``max_action``) fail closed —
never a guess.  The classification functions are pure: the caller supplies the
tradable-hours flag and the alert ``max_action`` projection so no clock or
database access lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)

from .kick_filter import SUPPORTED_MARKETS, KickVerdict

BUY_REVIEW_INTENT = "buy_review"
APPROVAL_REQUIRED_ACTION_MODE = "approval_required"
# The investment_watch_events action_mode CHECK constraint domain. Unknown
# strings classify queue-only under their own name rather than guessing.
_WATCH_ACTION_MODES = frozenset(
    {"notify_only", "preview_only", "approval_required", "auto_execute_mock"}
)
# Exchange calendars mirror the investment watch scanner's market-open gate
# (regular session minutes); crypto trades around the clock.
_TRADABLE_CALENDARS = {"kr": "XKRX", "us": "XNYS"}


@dataclass(frozen=True)
class WatchKickCursor:
    """Delivery-order cursor for the kick pass; same shape as WatchCursor."""

    delivered_at: datetime | None
    event_id: int


def is_tradable_now(market: str, now: datetime) -> bool:
    """True iff ``market`` is inside a tradable minute at ``now``.

    Crypto trades 24/7.  ``kr``/``us`` consult the XKRX/XNYS trading-minute
    calendars — the same gate the investment watch scanner applies.  Any
    calendar error (unknown exchange, out-of-range date) fails closed: a
    market whose hours cannot be confirmed is never kick-eligible.
    """
    if market == "crypto":
        return True
    name = _TRADABLE_CALENDARS.get(market)
    if name is None:
        return False
    try:
        import exchange_calendars as xcals
        import pandas as pd

        calendar = xcals.get_calendar(name)
        stamp = pd.Timestamp(now.astimezone(UTC)).floor("min")
        return bool(calendar.is_trading_minute(stamp.tz_convert(calendar.tz)))
    except Exception:  # noqa: BLE001 - unverifiable hours are closed hours
        return False


def classify_watch_without_action(event: Any, *, tradable: bool) -> KickVerdict | None:
    """Verdict for classes that never need the alert ``max_action`` read.

    Returns ``None`` only for an approval-required, in-hours event whose intent
    is not ``buy_review`` — the side branch must then be proven from the source
    alert's ``max_action``.
    """
    market = str(event.get("market") or "").strip().lower()
    if market not in SUPPORTED_MARKETS:
        return KickVerdict(False, "unsupported_market")
    action_mode = str(event.get("action_mode") or "").strip().lower()
    if action_mode != APPROVAL_REQUIRED_ACTION_MODE:
        if not action_mode:
            return KickVerdict(False, "action_mode_missing")
        if action_mode in _WATCH_ACTION_MODES:
            return KickVerdict(False, f"action_mode_{action_mode}")
        return KickVerdict(False, "action_mode_unknown")
    if not tradable:
        return KickVerdict(False, "market_closed")
    intent = str(event.get("intent") or "").strip().lower()
    if intent == BUY_REVIEW_INTENT:
        return KickVerdict(True, BUY_REVIEW_INTENT)
    return None


def classify_watch_for_kick(
    event: Any, alert_max_action: Any, *, tradable: bool
) -> KickVerdict:
    """Classify one delivered watch event for kick priority.

    ``alert_max_action`` is the LEFT JOIN projection of the source alert's
    ``max_action`` — ``None`` means the alert link is gone or unreadable, which
    can never prove a side and therefore fails closed to queue-only.
    """
    early = classify_watch_without_action(event, tradable=tradable)
    if early is not None:
        return early
    if not isinstance(alert_max_action, dict):
        return KickVerdict(False, "max_action_unavailable")
    side = str(alert_max_action.get("side") or "").strip()
    if side:
        return KickVerdict(True, "action_side")
    return KickVerdict(False, "no_action_side")


class DbWatchKickSource:
    """Read-only projection of delivered watch events plus alert max_action.

    Delivery ordering and the ``delivered`` filter mirror
    ``DbWatchAlertSource``; the LEFT JOIN supplies the ``max_action`` side
    signal the event snapshot does not carry.  A deleted alert (``alert_id``
    is ON DELETE SET NULL) projects ``None`` and fails closed downstream.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def high_watermark(self) -> WatchKickCursor:
        result = await self._db.execute(
            select(InvestmentWatchEvent.delivered_at, InvestmentWatchEvent.id)
            .where(
                InvestmentWatchEvent.delivery_status == "delivered",
                InvestmentWatchEvent.delivered_at.is_not(None),
            )
            .order_by(
                InvestmentWatchEvent.delivered_at.desc(),
                InvestmentWatchEvent.id.desc(),
            )
            .limit(1)
        )
        row = result.first()
        if row is None:
            return WatchKickCursor(None, 0)
        return WatchKickCursor(row.delivered_at, int(row.id))

    async def list_after(
        self, cursor: WatchKickCursor, *, limit: int
    ) -> list[dict[str, Any]]:
        if cursor.delivered_at is None:
            after_cursor = InvestmentWatchEvent.id > cursor.event_id
        else:
            after_cursor = or_(
                InvestmentWatchEvent.delivered_at > cursor.delivered_at,
                and_(
                    InvestmentWatchEvent.delivered_at == cursor.delivered_at,
                    InvestmentWatchEvent.id > cursor.event_id,
                ),
            )
        result = await self._db.execute(
            select(InvestmentWatchEvent, InvestmentWatchAlert.max_action)
            .outerjoin(
                InvestmentWatchAlert,
                InvestmentWatchEvent.alert_id == InvestmentWatchAlert.id,
            )
            .where(
                after_cursor,
                InvestmentWatchEvent.delivery_status == "delivered",
                InvestmentWatchEvent.delivered_at.is_not(None),
            )
            .order_by(
                InvestmentWatchEvent.delivered_at.asc(),
                InvestmentWatchEvent.id.asc(),
            )
            .limit(max(1, min(int(limit), 500)))
        )
        return [
            _watch_kick_dict(event, alert_max_action)
            for event, alert_max_action in result.all()
        ]


def _watch_kick_dict(row: Any, alert_max_action: Any) -> dict[str, Any]:
    return {
        "event_id": int(row.id),
        "event_uuid": str(row.event_uuid),
        "idempotency_key": row.idempotency_key,
        "alert_id": row.alert_id,
        "market": row.market,
        "symbol": row.symbol,
        "metric": row.metric,
        "operator": row.operator,
        "threshold": str(row.threshold),
        "threshold_high": (
            None if row.threshold_high is None else str(row.threshold_high)
        ),
        "current_value": None if row.current_value is None else str(row.current_value),
        "outcome": row.outcome,
        "intent": row.intent,
        "action_mode": row.action_mode,
        "kst_date": row.kst_date,
        "correlation_id": row.correlation_id,
        "delivered_at": (
            None if row.delivered_at is None else row.delivered_at.isoformat()
        ),
        "alert_max_action": (
            alert_max_action if isinstance(alert_max_action, dict) else None
        ),
    }


__all__ = [
    "APPROVAL_REQUIRED_ACTION_MODE",
    "BUY_REVIEW_INTENT",
    "DbWatchKickSource",
    "WatchKickCursor",
    "classify_watch_for_kick",
    "classify_watch_without_action",
    "is_tradable_now",
]
