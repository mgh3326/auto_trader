"""ROB-843 P1 — write-ahead durable reservation for KIS mock scalping order legs.

The reservation is inserted into ``review.order_send_intents`` BEFORE the broker
POST, so a DB that cannot record the send never reaches the network. It is
released only when the order is confirmed fully tracked or proven not sent; an
UNRESOLVED reservation is the durable "in-flight / uncertain" state that
survives process restart and fail-closes new orders until an explicit
reconciliation releases it (unlike the old post-POST ledger marker, which shared
the native write's failure mode). No writes to the order ledger — no control
rows to leak into retrospective / journal / holdings consumers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from app.services.order_send_intent_service import (
    KIS_MOCK_SCALPING_SCOPE,
    OrderSendIntentService,
)

logger = logging.getLogger("rob321.kis_mock_scalping_exec")

ReservationSide = Literal["buy", "sell"]
_LEG_KEY_PREFIX = "leg:v1:"


def _session_factory():
    from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory

    return _order_session_factory()


def _normalize_side(side: str) -> ReservationSide:
    normalized = side.strip().lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError(f"unsupported reservation side: {side!r}")
    return normalized  # type: ignore[return-value]


def _leg_key(*, correlation_id: str, side: str) -> str:
    if not correlation_id:
        raise ValueError("correlation_id is required")
    normalized_side = _normalize_side(side)
    return f"{_LEG_KEY_PREFIX}{normalized_side}:{correlation_id}"


def _decode_leg_key(
    *, stored_key: str, stored_side: str | None
) -> tuple[str, ReservationSide]:
    """Decode a current leg key; retain reconciliation access to legacy raw keys."""
    if stored_key.startswith(_LEG_KEY_PREFIX):
        encoded_side, separator, correlation_id = stored_key[
            len(_LEG_KEY_PREFIX) :
        ].partition(":")
        if not separator or not correlation_id:
            raise ValueError(f"malformed reservation leg key: {stored_key!r}")
        side = _normalize_side(encoded_side)
        if stored_side is not None and _normalize_side(stored_side) != side:
            raise ValueError(f"reservation side mismatch: {stored_key!r}")
        return correlation_id, side

    # A pre-leg-key reservation has its correlation ID as the raw key and its
    # leg in the existing side column. Reconciliation can still identify it
    # honestly and delete that exact stored key without a schema migration.
    if stored_side is None:
        raise ValueError(f"legacy reservation is missing side: {stored_key!r}")
    return stored_key, _normalize_side(stored_side)


async def reserve_entry(*, correlation_id: str, symbol: str, side: str) -> None:
    """Reserve a BUY or SELL leg BEFORE the POST.

    Raises ``DuplicateOrderIntent`` on a same-key
    resend and any other exception on a durable-write failure — both must abort
    the send (the caller returns before the broker POST)."""
    normalized_side = _normalize_side(side)
    async with _session_factory()() as db:
        await OrderSendIntentService(db).reserve(
            account_scope=KIS_MOCK_SCALPING_SCOPE,
            idempotency_key=_leg_key(
                correlation_id=correlation_id, side=normalized_side
            ),
            symbol=symbol,
            side=normalized_side,
            # Compatibility with pre-leg-key rows from 9bb74194. Only the same
            # normalized leg conflicts, so an unresolved legacy BUY still lets
            # its safety-critical SELL close path reserve independently.
            conflicting_key_sides=((correlation_id, normalized_side),),
        )


async def _release_stored_key(stored_key: str) -> int:
    async with _session_factory()() as db:
        return await OrderSendIntentService(db).release(
            account_scope=KIS_MOCK_SCALPING_SCOPE,
            idempotency_key=stored_key,
        )


async def release_entry(*, correlation_id: str, side: str) -> int:
    return await _release_stored_key(_leg_key(correlation_id=correlation_id, side=side))


async def has_unresolved_entries() -> bool:
    async with _session_factory()() as db:
        return await OrderSendIntentService(db).has_reservations(
            account_scope=KIS_MOCK_SCALPING_SCOPE
        )


async def reconcile_entries(
    *, confirm: Callable[[str, ReservationSide], Awaitable[bool]]
) -> int:
    """Release only the exact ``(correlation_id, side)`` leg broker confirms.

    Unconfirmed or malformed reservations stay, keeping the global trading gate
    fail-closed. Returns the number released.
    """
    async with _session_factory()() as db:
        reservations = await OrderSendIntentService(db).list_keys_and_sides(
            account_scope=KIS_MOCK_SCALPING_SCOPE
        )
    released = 0
    for stored_key, stored_side in reservations:
        try:
            correlation_id, side = _decode_leg_key(
                stored_key=stored_key, stored_side=stored_side
            )
        except ValueError as exc:
            logger.warning("malformed scalping reservation kept unresolved: %s", exc)
            continue
        if await confirm(correlation_id, side):
            await _release_stored_key(stored_key)
            released += 1
    return released
