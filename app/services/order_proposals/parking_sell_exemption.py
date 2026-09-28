"""Proposal-bound authority for an ordinary parking-instrument limit sell.

Only auto dispatch constructs this context from a persisted proposal/rung.
Direct MCP order calls cannot supply it.  The bound account is compared with
the broker-selected account again at preview and submit; no account is inferred
from settings for the proposal itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.services.market_events.session_calendar import regular_session_bounds
from app.services.order_proposals.parking_allowlist import parking_scope

# The library XKRX calendar currently reports the ordinary 09:00 opening on
# these exam days. KRX delayed the 2025 regular opening until 10:00; the 2026
# exam date is published, but its exchange notice is not yet available. Apply
# the same conservative lower bound so this auto-approval cannot use pre-open.
_KRX_CONSERVATIVE_LATE_OPEN_DATES = frozenset((date(2025, 11, 13), date(2026, 11, 19)))


def explicit_account_matches(account_mode: Any, broker_account_id: Any) -> bool:
    """Require an exact caller-provided ID for the account the broker selects."""
    if type(broker_account_id) is not str or not broker_account_id:
        return False
    if account_mode == "toss_live":
        configured = settings.toss_api_account_seq
        return (
            type(configured) is int
            and configured > 0
            and broker_account_id == str(configured)
        )
    if account_mode == "kis_live":
        configured = settings.kis_account_no
        return (
            type(configured) is str
            and bool(configured)
            and broker_account_id == configured
        )
    return False


def kr_regular_session_open(market: Any, now: datetime | None) -> bool:
    """The XKRX calendar includes holidays and shortened regular sessions."""
    if market != "equity_kr":
        return True
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return False
    instant = now.astimezone(UTC)
    from zoneinfo import ZoneInfo

    local_now = now.astimezone(ZoneInfo("Asia/Seoul"))
    if (
        local_now.date() in _KRX_CONSERVATIVE_LATE_OPEN_DATES
        and local_now.time() < time(10)
    ):
        return False
    bounds = regular_session_bounds("kr", local_now.date())
    return bounds is not None and bounds[0] <= instant < bounds[1]


def _decimal(value: Any) -> Decimal | None:
    if type(value) not in (Decimal, int, float, str):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _proposal_uuid(value: Any) -> uuid.UUID | None:
    if type(value) is uuid.UUID:
        return value
    if value is None:
        return None
    # Keep default-off TaskIQ registration free of the asyncpg import. This
    # codec type is needed only while binding a persisted proposal UUID.
    from asyncpg.pgproto.pgproto import UUID as AsyncpgUUID

    if type(value) is AsyncpgUUID:
        # PostgreSQL's UUID codec returns this exact built-in extension type.
        # String conversion is limited to that trusted DB type, never a caller
        # subclass with an arbitrary __str__ implementation.
        return uuid.UUID(str(value))
    return None


@dataclass(frozen=True)
class ParkingSellContext:
    proposal_id: uuid.UUID
    rung_index: int
    symbol: str
    market: str
    account_mode: str
    broker_account_id: str
    quantity: Decimal
    limit_price: Decimal

    def matches(
        self,
        *,
        symbol: Any,
        market: Any,
        account_mode: Any,
        side: Any,
        order_type: Any,
        quantity: Any,
        price: Any,
    ) -> bool:
        return (
            type(self.proposal_id) is uuid.UUID
            and type(self.rung_index) is int
            and self.rung_index >= 0
            and side == "sell"
            and order_type == "limit"
            and symbol == self.symbol
            and market == self.market
            and account_mode == self.account_mode
            and _decimal(quantity) == self.quantity
            and _decimal(price) == self.limit_price
            and parking_scope(symbol=symbol, account_mode=account_mode, market=market)
            is not None
            and explicit_account_matches(account_mode, self.broker_account_id)
        )

    def send_ready(self, now: datetime | None = None) -> bool:
        return explicit_account_matches(
            self.account_mode, self.broker_account_id
        ) and kr_regular_session_open(
            self.market, now if now is not None else datetime.now(UTC)
        )


def bind_parking_sell_context(group: Any, rung: Any) -> ParkingSellContext | None:
    """Construct only after loading the persisted proposal and rung."""
    symbol = getattr(group, "symbol", None)
    market = getattr(group, "market", None)
    account_mode = getattr(group, "account_mode", None)
    broker_account_id = getattr(group, "broker_account_id", None)
    quantity = _decimal(getattr(rung, "quantity", None))
    limit_price = _decimal(getattr(rung, "limit_price", None))
    proposal_id = _proposal_uuid(getattr(group, "proposal_id", None))
    rung_index = getattr(rung, "rung_index", None)
    if not (
        type(proposal_id) is uuid.UUID
        and type(rung_index) is int
        and rung_index >= 0
        and (getattr(group, "action", None) or "place") == "place"
        and getattr(group, "exit_intent", None) is None
        and getattr(group, "order_type", None) == "limit"
        and getattr(rung, "side", None) == "sell"
        and parking_scope(symbol=symbol, account_mode=account_mode, market=market)
        is not None
        and explicit_account_matches(account_mode, broker_account_id)
        and quantity is not None
        and limit_price is not None
    ):
        return None
    return ParkingSellContext(
        proposal_id,
        rung_index,
        symbol,
        market,
        account_mode,
        broker_account_id,
        quantity,
        limit_price,
    )
