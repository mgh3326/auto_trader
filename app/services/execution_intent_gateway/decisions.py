"""ROB-1440 — the gateway's refusal rules as pure functions.

No DB, no network, no clock. The store calls :func:`decide` while it holds the
intent row lock, so ``children`` is complete for that intent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from app.schemas.execution_intent import AmendChild, CancelChild, SubmitChild
from app.services.kis_mock_runner.control import KillMode
from app.services.kis_mock_runner.envelope import LOCKED_ENVELOPE

MOCK_ACCOUNTS: Final[frozenset[str]] = frozenset({"kis_mock"})

Child = SubmitChild | AmendChild | CancelChild


class ChildState(StrEnum):
    SENDING = "sending"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"


class Refusal(StrEnum):
    GATEWAY_DISABLED = "gateway_disabled"
    CONFIRM_REQUIRED = "confirm_required"
    INTENT_NOT_FOUND = "intent_not_found"
    ACCOUNT_NOT_MOCK = "account_not_mock"
    FORWARDER_UNAVAILABLE = "forwarder_unavailable"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    SUBMISSIONS_KILLED = "submissions_killed"
    PRICE_OUTSIDE_BOUND = "price_outside_bound"
    QUANTITY_CAP_EXCEEDED = "quantity_cap_exceeded"
    NOTIONAL_CAP_EXCEEDED = "notional_cap_exceeded"
    LOCKED_ENVELOPE_CAP_EXCEEDED = "locked_envelope_cap_exceeded"
    INVENTORY_CONSTRAINT_EXCEEDED = "inventory_constraint_exceeded"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_NOT_WORKING = "target_not_working"
    TARGET_SUPERSEDED = "target_superseded"


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    """A stored intent as read. Deliberately not re-validated by the schema, so
    the refusals below still hold for a row a later schema version wrote.
    """

    intent_id: UUID
    strategy: str
    account: str
    venue: str
    symbol: str
    side: str
    max_quantity: int
    max_notional_krw: int
    limit_price_krw: int
    max_slippage_bps: int
    position_at_authorization: int
    min_position_after: int
    max_position_after: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ChildSnapshot:
    child_id: int
    idempotency_key: str
    request_digest: str
    action: str
    target_idempotency_key: str | None
    quantity: int | None
    limit_price_krw: int | None
    state: ChildState
    refusal_reason: str | None
    broker_order_id: str | None
    result: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class Decision:
    """``refusal is None`` allows the forward."""

    refusal: Refusal | None = None
    target_broker_order_id: str | None = None


def _price_within_bound(intent: IntentSnapshot, price: int) -> bool:
    """A buy may pay up to the bound and a sell may give up to it, never further."""
    scaled = price * 10_000
    if intent.side == "buy":
        return scaled <= intent.limit_price_krw * (10_000 + intent.max_slippage_bps)
    if intent.side == "sell":
        return scaled >= intent.limit_price_krw * (10_000 - intent.max_slippage_bps)
    return False


def _root_of(key: str, by_key: dict[str, ChildSnapshot]) -> str:
    seen: set[str] = set()
    current = key
    while current not in seen:
        seen.add(current)
        row = by_key.get(current)
        if row is None or row.target_idempotency_key is None:
            return current
        current = row.target_idempotency_key
    return current


def _exposure_by_order(
    children: Sequence[ChildSnapshot],
) -> dict[str, tuple[int, int]]:
    """Quantity and notional each order may have reached, keyed by its submit.

    Each order counts at its high-water mark, because an amend with an unknown
    outcome may or may not have applied. Every row that was not refused counts,
    including cancelled and failed ones: without fill evidence the gateway cannot
    prove the quantity is free again, so an intent's caps bound everything it ever
    sent.
    """
    by_key = {child.idempotency_key: child for child in children}
    exposure: dict[str, tuple[int, int]] = {}
    for child in children:
        if (
            child.state is ChildState.REFUSED
            or child.quantity is None
            or child.limit_price_krw is None
        ):
            continue
        root = _root_of(child.idempotency_key, by_key)
        quantity, notional = exposure.get(root, (0, 0))
        exposure[root] = (
            max(quantity, child.quantity),
            max(notional, child.quantity * child.limit_price_krw),
        )
    return exposure


def _caps_refusal(
    intent: IntentSnapshot,
    *,
    children: Sequence[ChildSnapshot],
    root: str,
    quantity: int,
    price: int,
) -> Refusal | None:
    notional = quantity * price
    if notional > LOCKED_ENVELOPE.max_single_notional_krw:
        return Refusal.LOCKED_ENVELOPE_CAP_EXCEEDED

    exposure = _exposure_by_order(children)
    held_quantity, held_notional = exposure.get(root, (0, 0))
    exposure[root] = (max(held_quantity, quantity), max(held_notional, notional))
    total_quantity = sum(value[0] for value in exposure.values())
    total_notional = sum(value[1] for value in exposure.values())

    if total_quantity > intent.max_quantity:
        return Refusal.QUANTITY_CAP_EXCEEDED
    if total_notional > intent.max_notional_krw:
        return Refusal.NOTIONAL_CAP_EXCEEDED

    start = intent.position_at_authorization
    projected = (
        start + total_quantity if intent.side == "buy" else start - total_quantity
    )
    if not intent.min_position_after <= projected <= intent.max_position_after:
        return Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    return None


def _decide_cancel(request: CancelChild, by_key: dict[str, ChildSnapshot]) -> Decision:
    target = by_key.get(request.target_idempotency_key)
    if target is None or target.action == "cancel":
        return Decision(refusal=Refusal.TARGET_NOT_FOUND)
    if target.broker_order_id is None:
        return Decision(refusal=Refusal.TARGET_NOT_WORKING)
    finished_by_later_child = any(
        row.target_idempotency_key == target.idempotency_key
        and row.state is ChildState.ACCEPTED
        for row in by_key.values()
    )
    if finished_by_later_child:
        return Decision(refusal=Refusal.TARGET_SUPERSEDED)
    return Decision(target_broker_order_id=target.broker_order_id)


def _decide_amend(
    request: AmendChild,
    intent: IntentSnapshot,
    children: Sequence[ChildSnapshot],
    by_key: dict[str, ChildSnapshot],
) -> Decision:
    target = by_key.get(request.target_idempotency_key)
    if target is None or target.action == "cancel":
        return Decision(refusal=Refusal.TARGET_NOT_FOUND)
    if target.state is not ChildState.ACCEPTED or target.broker_order_id is None:
        return Decision(refusal=Refusal.TARGET_NOT_WORKING)
    # Any later child that was not refused may already have replaced or cancelled
    # this order, so its broker order id can no longer be trusted.
    if any(
        row.target_idempotency_key == target.idempotency_key
        and row.state is not ChildState.REFUSED
        for row in by_key.values()
    ):
        return Decision(refusal=Refusal.TARGET_SUPERSEDED)
    refusal = _caps_refusal(
        intent,
        children=children,
        root=_root_of(target.idempotency_key, by_key),
        quantity=request.quantity,
        price=request.limit_price_krw,
    )
    if refusal is not None:
        return Decision(refusal=refusal)
    return Decision(target_broker_order_id=target.broker_order_id)


def decide(
    request: Child,
    *,
    intent: IntentSnapshot,
    children: Sequence[ChildSnapshot],
    now: datetime,
    kill_mode: KillMode | None,
    forwarder_accounts: frozenset[str],
) -> Decision:
    """Refuse or allow one new child request.

    A cancel does not consult expiry, kill state or caps: it only ever reduces
    exposure, and a stuck working order is the worse failure. ``kill_mode`` is
    None only for a cancel, which never reads it.
    """
    if intent.account not in MOCK_ACCOUNTS:
        return Decision(refusal=Refusal.ACCOUNT_NOT_MOCK)
    if intent.account not in forwarder_accounts:
        return Decision(refusal=Refusal.FORWARDER_UNAVAILABLE)

    by_key = {child.idempotency_key: child for child in children}
    if isinstance(request, CancelChild):
        return _decide_cancel(request, by_key)

    if now >= intent.expires_at:
        return Decision(refusal=Refusal.AUTHORIZATION_EXPIRED)
    if kill_mode is not KillMode.ACTIVE:
        return Decision(refusal=Refusal.SUBMISSIONS_KILLED)
    if not _price_within_bound(intent, request.limit_price_krw):
        return Decision(refusal=Refusal.PRICE_OUTSIDE_BOUND)

    if isinstance(request, AmendChild):
        return _decide_amend(request, intent, children, by_key)

    return Decision(
        refusal=_caps_refusal(
            intent,
            children=children,
            root=request.idempotency_key,
            quantity=request.quantity,
            price=request.limit_price_krw,
        )
    )
