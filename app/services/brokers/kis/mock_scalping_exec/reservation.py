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

from collections.abc import Awaitable, Callable

from app.services.order_send_intent_service import (
    KIS_MOCK_SCALPING_SCOPE,
    OrderSendIntentService,
)


def _session_factory():
    from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory

    return _order_session_factory()


async def reserve_entry(*, correlation_id: str, symbol: str, side: str) -> None:
    """Reserve a BUY or SELL leg BEFORE the POST.

    Raises ``DuplicateOrderIntent`` on a same-key
    resend and any other exception on a durable-write failure — both must abort
    the send (the caller returns before the broker POST)."""
    async with _session_factory()() as db:
        await OrderSendIntentService(db).reserve(
            account_scope=KIS_MOCK_SCALPING_SCOPE,
            idempotency_key=correlation_id,
            symbol=symbol,
            side=side,
        )


async def release_entry(*, correlation_id: str) -> int:
    async with _session_factory()() as db:
        return await OrderSendIntentService(db).release(
            account_scope=KIS_MOCK_SCALPING_SCOPE,
            idempotency_key=correlation_id,
        )


async def has_unresolved_entries() -> bool:
    async with _session_factory()() as db:
        return await OrderSendIntentService(db).has_reservations(
            account_scope=KIS_MOCK_SCALPING_SCOPE
        )


async def reconcile_entries(*, confirm: Callable[[str], Awaitable[bool]]) -> int:
    """Explicit reconciliation: release only reservations that ``confirm`` proves
    resolved (broker evidence). Unconfirmed reservations stay, keeping trading
    fail-closed. Returns the number released."""
    async with _session_factory()() as db:
        keys = await OrderSendIntentService(db).list_keys(
            account_scope=KIS_MOCK_SCALPING_SCOPE
        )
    released = 0
    for key in keys:
        if await confirm(key):
            await release_entry(correlation_id=key)
            released += 1
    return released
