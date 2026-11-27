"""hk 6991 — the AT intent issuer: a published daily_plan row → one v3 intent.

Design 6939 §1.3 and §5. The issuer reads only an ``executable`` row of a
``PUBLISHED`` version of a head whose day mode is ``executable`` (policy key
``daily_plan.executable_markets``), and writes one immutable v3 intent bound to
``(plan_head_id, plan_version, row_key)``. An entry row (``new``/``add``) and a
trim (target) row each get their own intent; every copied value comes from the
bound row only.

The issuer never sends anything: it only stores the intent through the gateway
store. Today the only account it issues for is ``kis_mock`` on KRX (the existing
forwarder); every other account or venue is refused, so no live account is
enabled by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.execution_intent import (
    MAX_INTENT_NOTIONAL_KRW,
    MAX_SLIPPAGE_BPS,
    ExecutionIntentV3,
)
from app.services.daily_plan.row_lock import lock_account

_NAMESPACE: Final = UUID("6991c0de-0000-5000-8000-000000006939")

ISSUABLE_ACTIONS: Final = frozenset({"new", "add", "trim"})
SUPPORTED_ACCOUNT_MODES: Final = frozenset({"kis_mock"})
SUPPORTED_VENUES: Final = frozenset({"KRX"})


class IssueRefused(ValueError):
    """The row cannot be issued. ``code`` is a closed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IntentWriter(Protocol):
    async def store_intent(
        self, intent: ExecutionIntentV3, *, now: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class IssueRequest:
    market: str
    day: date
    seq: int
    row_key: tuple[str, str, str, str | None]
    #: The account position the intent assumes (the broker-confirmed holding).
    position: int
    #: Ledger-confirmed fills of earlier intents bound to this row (design §3 I9):
    #: the new intent's budget is the approved size minus these, never reset.
    prior_filled_quantity: int = 0


def _whole_krw(value: Any) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IssueRefused("price_not_whole_krw") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise IssueRefused("price_not_whole_krw")
    return int(number)


def _quantity(body: dict[str, Any], limit: int) -> int:
    if body.get("would_be_quantity") is not None:
        quantity = Decimal(str(body["would_be_quantity"]))
        if quantity != quantity.to_integral_value():
            raise IssueRefused("quantity_not_whole")
        return int(quantity)
    if body.get("would_be_notional") is not None:
        return int(Decimal(str(body["would_be_notional"])) // limit)
    raise IssueRefused("size_missing")


def _invalidation_price(body: dict[str, Any]) -> int:
    for condition in body.get("invalidation_conditions") or []:
        if condition.get("kind") == "price_at_or_below":
            return _whole_krw(condition["value"])
    raise IssueRefused("invalidation_missing")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise IssueRefused("expiry_unreadable")
    return parsed


def build_intent(
    *,
    version: dict[str, Any],
    row: dict[str, Any],
    request: IssueRequest,
    now: datetime,
) -> ExecutionIntentV3:
    """Pure: the v3 intent for one issuable row, or :class:`IssueRefused`."""
    body = row["body"]
    action = row["action"]
    if action not in ISSUABLE_ACTIONS:
        raise IssueRefused("row_action_not_issuable")
    if body.get("disposition") == "dropped" or body.get("not_executable_reason"):
        raise IssueRefused("row_blocked")
    mode = body.get("would_be_account_mode") or body.get("account_mode")
    if mode not in SUPPORTED_ACCOUNT_MODES:
        raise IssueRefused("account_not_supported")
    account_ref = lock_account(body)
    if not account_ref:
        raise IssueRefused("account_unassigned")
    venue = body.get("venue")
    if venue not in SUPPORTED_VENUES:
        raise IssueRefused("venue_not_supported")

    entry = action in ("new", "add")
    if entry:
        limit = _whole_krw(body.get("would_be_limit_price"))
        ceiling = _whole_krw(body.get("max_buy_price"))
        slippage = min(MAX_SLIPPAGE_BPS, (ceiling - limit) * 10_000 // limit)
        invalidation: int | None = _invalidation_price(body)
    else:
        limit = _whole_krw(body.get("min_sell_price"))
        slippage = 0
        invalidation = None
    approved = _quantity(body, limit)
    if request.prior_filled_quantity < 0:
        raise IssueRefused("prior_fills_invalid")
    quantity = approved - request.prior_filled_quantity
    if quantity < 1:
        raise IssueRefused("no_remaining_quantity")
    if quantity * limit > MAX_INTENT_NOTIONAL_KRW:
        raise IssueRefused("notional_cap_exceeded")
    position = request.position
    if position < 0 or (not entry and position < quantity):
        raise IssueRefused("position_insufficient")
    expires_at = _parse_time(body["valid_until"])
    if expires_at <= now:
        raise IssueRefused("row_expired")

    head_id = version["head_id"]
    seq = version["seq"]
    key = tuple(request.row_key)
    intent_id = uuid5(
        _NAMESPACE, f"{head_id}:{seq}:{list(key)}:{request.prior_filled_quantity}"
    )
    try:
        return ExecutionIntentV3.model_validate(
            {
                "schema_version": 3,
                "intent_id": intent_id,
                "strategy": version["strategy_id"],
                "account": "kis_mock",
                "venue": "krx",
                "symbol": row["symbol"],
                "side": "buy" if entry else "sell",
                "max_quantity": quantity,
                "max_notional_krw": quantity * limit,
                "limit_price_krw": limit,
                "max_slippage_bps": int(slippage),
                "expires_at": expires_at,
                "inventory": {
                    "position_at_authorization": position,
                    "min_position_after": position if entry else position - quantity,
                    "max_position_after": position + quantity if entry else position,
                },
                "policy_version": version["policy_version"],
                "authorization_ref": f"daily-plan:{head_id}:{seq}",
                "plan": {
                    "plan_head_id": head_id,
                    "plan_version": seq,
                    "row_key": key,
                    "account_ref": account_ref,
                    "venue": venue,
                    "session": body["session"],
                    "action": action,
                    "invalidation_price_krw": invalidation,
                },
            }
        )
    except ValidationError as exc:
        raise IssueRefused("intent_invalid") from exc


async def issue_intent(
    db: AsyncSession,
    writer: IntentWriter,
    *,
    request: IssueRequest,
    policy: Any,
    now: datetime,
) -> ExecutionIntentV3:
    """Read the row (refusing anything not issuable) and store its v3 intent."""
    from app.services.daily_plan.store import RowNotIssuable, read_issuable_row

    try:
        found = await read_issuable_row(
            db,
            market=request.market,
            day=request.day,
            seq=request.seq,
            key=request.row_key,
            policy=policy,
        )
    except RowNotIssuable as exc:
        raise IssueRefused(str(exc)) from exc
    intent = build_intent(
        version=found["version"], row=found["row"], request=request, now=now
    )
    await writer.store_intent(intent, now=now)
    return intent
