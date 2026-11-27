"""hk 6991 — v3 intents: supersession at reservation and pre-send (design 6939 §5).

A v3 intent is bound to one published daily_plan row ``(plan_head_id,
plan_version, row_key)``. Two refusals follow from that binding:

* ``plan_version_stale`` — the latest published seq in which the bound row's
  execution fields changed is greater than the intent's ``plan_version``.
  Staleness is per row: a publish that changes other rows, or a ``fill``-kind
  version, leaves the intent sendable.
* ``prior_intent_unsettled`` — a new submit while another v3 intent on the same
  (account, symbol, venue) has a send in flight (``sending``/``uncertain``) or an
  order without terminal ledger evidence. Uncertain is never resent.

Both are decided under the per-row advisory lock of
:mod:`app.services.daily_plan.row_lock`, taken by the child reservation (inside
the reservation transaction) and by the pre-send hook, which holds it through
the adapter's first send. A cancel is never checked here: old-version orders
stay cancellable (ROB-1440 rule).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.execution_intent import ExecutionIntentV3
from app.services.daily_plan.row_lock import lock_plan_row
from app.services.execution_intent_gateway.decisions import (
    NEVER_SENT_STATES,
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
    _root_of,
)

logger = logging.getLogger(__name__)

#: Why a reserved v3 child was proven never to have reached the broker. The
#: journal trigger accepts ``sending -> not_sent`` on a v3 intent only with one
#: of these; a test pins the two lists.
V3_NOT_SENT_REASONS: Final[frozenset[str]] = frozenset(
    {Refusal.PLAN_VERSION_STALE.value, Refusal.PRIOR_INTENT_UNSETTLED.value}
)

#: kis_mock ledger lifecycle states that prove an order is finished and its
#: fills reconciled. Anything else (open, stale, failed, anomaly, absent) is not.
SETTLED_LEDGER_STATES: Final = frozenset({"reconciled", "cancelled", "expired"})


def row_key_text(key: Sequence[str | None]) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def row_key_of(intent: IntentSnapshot) -> tuple[str, str, str, str | None]:
    if intent.plan_row_key is None:
        raise ValueError("intent has no plan binding")
    kind, market, symbol, account = json.loads(intent.plan_row_key)
    return kind, market, symbol, account


def is_plan_bound(intent: IntentSnapshot) -> bool:
    return intent.schema_version == 3


def order_roots(children: Sequence[ChildSnapshot]) -> set[str]:
    """Orders the intent may have placed, by their submit key."""
    by_key = {child.idempotency_key: child for child in children}
    return {
        _root_of(child.idempotency_key, by_key)
        for child in children
        if child.action in ("submit", "amend") and child.state not in NEVER_SENT_STATES
    }


def intent_unsettled(
    children: Sequence[ChildSnapshot], terminal: Mapping[str, bool]
) -> bool:
    """Whether a prior intent still has anything open or unknown.

    ``terminal`` maps an order root to whether ledger evidence proves it
    finished; a missing root is not proven.
    """
    if any(c.state in (ChildState.SENDING, ChildState.UNCERTAIN) for c in children):
        return True
    return any(terminal.get(root) is not True for root in order_roots(children))


def plan_refusal(
    intent: IntentSnapshot,
    *,
    action: str,
    latest_change_seq: int | None,
    prior_unsettled: bool,
) -> Refusal | None:
    """The v3 decision. ``None`` allows the child (other rules still apply)."""
    if action == "cancel":
        return None
    if (
        latest_change_seq is None
        or intent.plan_version is None
        or latest_change_seq > intent.plan_version
    ):
        # A row that has no published change at all cannot be the row the intent
        # was issued from: refuse as stale rather than trust the binding.
        return Refusal.PLAN_VERSION_STALE
    if action == "submit" and prior_unsettled:
        return Refusal.PRIOR_INTENT_UNSETTLED
    return None


class PlanFacts(Protocol):
    """What the v3 rule reads, inside a transaction holding the row lock."""

    async def latest_change_seq(
        self, session: AsyncSession, intent: IntentSnapshot
    ) -> int | None: ...

    async def prior_unsettled(
        self, session: AsyncSession, intent: IntentSnapshot
    ) -> bool: ...


class PlanGuard(Protocol):
    async def at_reservation(
        self, session: AsyncSession, intent: IntentSnapshot, action: str
    ) -> Refusal | None: ...

    def pre_send(
        self, intent: IntentSnapshot, action: str
    ) -> AbstractAsyncContextManager[Refusal | None]: ...


class SettlementReader(Protocol):
    async def terminal(
        self, session: AsyncSession, roots: Iterable[str]
    ) -> dict[str, bool]: ...


class KisMockLedgerSettlement:
    """Terminal evidence of gateway-origin kis_mock orders, from the order ledger.

    An order is finished when every ledger row correlated to its root key is in
    :data:`SETTLED_LEDGER_STATES`, and there is at least one. No row is not
    proven: the ledger row is written after the send.
    """

    async def terminal(
        self, session: AsyncSession, roots: Iterable[str]
    ) -> dict[str, bool]:
        from app.models.review import KISMockOrderLedger
        from app.services.execution_intent_gateway.kis_mock_forwarder import (
            order_correlation_id,
        )

        by_correlation = {order_correlation_id(root): root for root in roots}
        if not by_correlation:
            return {}
        result = await session.execute(
            select(
                KISMockOrderLedger.correlation_id, KISMockOrderLedger.lifecycle_state
            ).where(KISMockOrderLedger.correlation_id.in_(list(by_correlation)))
        )
        terminal: dict[str, bool] = {}
        for correlation, state in result:
            root = by_correlation[correlation]
            terminal[root] = terminal.get(root, True) and state in SETTLED_LEDGER_STATES
        return terminal


class SqlPlanFacts:
    def __init__(self, settlement: SettlementReader) -> None:
        self._settlement = settlement

    async def latest_change_seq(
        self, session: AsyncSession, intent: IntentSnapshot
    ) -> int | None:
        from app.services.daily_plan.store import row_change_seq

        if intent.plan_head_id is None:
            return None
        return await row_change_seq(
            session, head_id=intent.plan_head_id, key=row_key_of(intent)
        )

    async def prior_unsettled(
        self, session: AsyncSession, intent: IntentSnapshot
    ) -> bool:
        from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
        from app.services.execution_intent_gateway.store import _child_snapshot

        others = list(
            await session.scalars(
                select(ExecutionIntent.id).where(
                    ExecutionIntent.schema_version == 3,
                    ExecutionIntent.plan_account_ref == intent.plan_account_ref,
                    ExecutionIntent.symbol == intent.symbol,
                    ExecutionIntent.plan_venue == intent.plan_venue,
                    ExecutionIntent.id != intent.intent_id,
                )
            )
        )
        for other in others:
            children = [
                _child_snapshot(row)
                for row in await session.scalars(
                    select(ExecutionChildRequest)
                    .where(ExecutionChildRequest.intent_id == other)
                    .order_by(ExecutionChildRequest.id)
                )
            ]
            roots = order_roots(children)
            terminal = await self._settlement.terminal(session, roots)
            if intent_unsettled(children, terminal):
                return True
        return False


async def decide_under_lock(
    session: AsyncSession, facts: PlanFacts, intent: IntentSnapshot, action: str
) -> Refusal | None:
    """Take the row lock in ``session`` and decide. Caller owns the transaction."""
    if action == "cancel":
        return None
    if intent.plan_account_ref is None or intent.plan_venue is None:
        return Refusal.PLAN_VERSION_STALE
    await lock_plan_row(
        session, intent.plan_account_ref, intent.symbol, intent.plan_venue
    )
    latest = await facts.latest_change_seq(session, intent)
    prior = action == "submit" and await facts.prior_unsettled(session, intent)
    return plan_refusal(
        intent, action=action, latest_change_seq=latest, prior_unsettled=prior
    )


class SqlPlanGuard:
    """The DB-backed guard. The reservation check runs in the store's own
    transaction; the pre-send check opens one and holds it through the send.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        facts: PlanFacts | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._facts = facts or SqlPlanFacts(KisMockLedgerSettlement())

    async def at_reservation(
        self, session: AsyncSession, intent: IntentSnapshot, action: str
    ) -> Refusal | None:
        return await decide_under_lock(session, self._facts, intent, action)

    @asynccontextmanager
    async def pre_send(
        self, intent: IntentSnapshot, action: str
    ) -> AsyncIterator[Refusal | None]:
        async with self._session_factory() as session:
            # The lock and the read share this transaction; it ends (and the
            # lock is released) only after the caller's first send returned.
            refusal = await decide_under_lock(session, self._facts, intent, action)
            try:
                yield refusal
            finally:
                await session.rollback()


def snapshot_of_v3(intent: ExecutionIntentV3) -> IntentSnapshot:
    """The stored form of a v3 intent, as :func:`store._intent_snapshot` reads it."""
    plan = intent.plan
    inventory = intent.inventory
    return IntentSnapshot(
        intent_id=intent.intent_id,
        strategy=intent.strategy,
        account=intent.account,
        venue=intent.venue,
        symbol=intent.symbol,
        side=intent.side,
        max_quantity=intent.max_quantity,
        max_notional_krw=intent.max_notional_krw,
        limit_price_krw=intent.limit_price_krw,
        max_slippage_bps=intent.max_slippage_bps,
        position_at_authorization=inventory.position_at_authorization,
        min_position_after=inventory.min_position_after,
        max_position_after=inventory.max_position_after,
        expires_at=intent.expires_at,
        schema_version=3,
        plan_head_id=plan.plan_head_id,
        plan_version=plan.plan_version,
        plan_row_key=row_key_text(plan.row_key),
        plan_account_ref=plan.account_ref,
        plan_venue=plan.venue,
        plan_session=plan.session,
        plan_action=plan.action,
        invalidation_price_krw=plan.invalidation_price_krw,
    )
