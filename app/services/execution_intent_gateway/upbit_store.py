"""hk 5441 — the journal side of the ``upbit_live`` account.

One short transaction per method; no session stays open across a broker call.

**Lock order, the same everywhere:** the sleeve advisory lock, then the mandate
row ``FOR UPDATE``, then the intent row ``FOR UPDATE``. The sleeve lock is the
one ``strategy_sleeves`` already uses for every decision about a sleeve, so a
reservation, a mandate revocation (B10), a handoff claim and a card dispatch of
the same sleeve never interleave. The facts a rule reads are loaded after all
three are held.

``reserve`` is the write-ahead step of design §1.1: it replays a known key, or
decides and commits the journal row. Two side effects ride in that same
transaction, so they exist exactly when the row that caused them does:

* a ``loss_sell_requires_card`` refusal of a live sell opens the S5 exit handoff;
* a loss-limit refusal appends the I8 ``stopped`` sleeve version (in a savepoint:
  the refusal holds even if that write fails).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.models.execution_mandates import ExecutionMandate, ExecutionOperatorExit
from app.models.strategy_sleeves import StrategySleeve
from app.schemas.execution_intent import (
    AnyChild,
    ExecutionIntentV2,
    contract_digest,
)
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.mandates import (
    is_revoked,
    lock_mandate,
    mandate_view,
    operator_exit_view,
)
from app.services.execution_intent_gateway.store import (
    MAX_INTENT_LIFETIME,
    IntentConflict,
    IntentNotStorable,
    _child_snapshot,
    _intent_snapshot,
)
from app.services.execution_intent_gateway.upbit_decisions import (
    UPBIT_ACCOUNTS,
    AccountReads,
    SleeveView,
    SymbolView,
    UpbitDecision,
    UpbitFacts,
    check_storable,
    child_notional,
)
from app.services.strategy_sleeves.book import SleeveBook, compute_sleeve_book
from app.services.strategy_sleeves.exit_handoffs import (
    active_handoff,
    open_exit_handoff,
)
from app.services.strategy_sleeves.intent_orders import (
    IntentChildOrder,
    child_order_key,
    load_intent_child_orders,
    shadow_book_inputs,
    unresolved_orders,
)
from app.services.strategy_sleeves.marks import MarkFn, latest_close_marks
from app.services.strategy_sleeves.store import (
    acquire_sleeve_lock,
    current_version,
    load_sleeve_inputs,
    sleeve_symbol,
    stop_at_loss_limit,
    version_view,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
#: Who opened a handoff the gateway opened on a refused sell.
GATEWAY_HANDOFF_ACTOR = "system:intent-gateway"

UpbitDecide = Callable[
    [IntentSnapshot, Sequence[ChildSnapshot], UpbitFacts], UpbitDecision
]


class IntentRefused(ValueError):
    """``store_intent`` refused the intent. Nothing was stored."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class UpbitReservation:
    """What reserving an Upbit child produced (the shape of ``store.Reservation``)."""

    intent: IntentSnapshot | None = None
    child: ChildSnapshot | None = None
    is_new: bool = False
    decision: UpbitDecision | None = None
    conflict: bool = False
    refusal_without_row: Refusal | None = None


def _replay_or_conflict(existing: ChildSnapshot, digest: str) -> UpbitReservation:
    if existing.request_digest != digest:
        return UpbitReservation(conflict=True)
    return UpbitReservation(child=existing, is_new=False)


def intent_snapshot_from_contract(
    intent: ExecutionIntentV2, *, created_at: datetime
) -> IntentSnapshot:
    inventory = intent.inventory
    return IntentSnapshot(
        intent_id=intent.intent_id,
        strategy=intent.strategy,
        account=intent.account,
        venue=intent.venue,
        symbol=intent.symbol,
        side=intent.side,
        max_quantity=0,
        max_notional_krw=intent.max_notional_krw,
        limit_price_krw=intent.limit_price_krw,
        max_slippage_bps=intent.max_slippage_bps,
        position_at_authorization=0,
        min_position_after=0,
        max_position_after=0,
        expires_at=intent.expires_at,
        schema_version=intent.schema_version,
        execution_mode=intent.execution_mode,
        mandate_id=intent.mandate_id,
        sleeve_id=intent.sleeve_id,
        strategy_code_sha=intent.strategy_code_sha,
        decision_ref=intent.decision_ref,
        origin=intent.origin,
        operator_exit_id=intent.operator_exit_id,
        max_quantity_decimal=Decimal(intent.max_quantity_decimal),
        position_at_authorization_decimal=Decimal(inventory.position_at_authorization),
        min_position_after_decimal=Decimal(inventory.min_position_after),
        max_position_after_decimal=Decimal(inventory.max_position_after),
        created_at=created_at,
    )


def sleeve_symbol_views(
    *,
    intent: IntentSnapshot,
    intents: Sequence[ExecutionIntent],
    orders: Sequence[IntentChildOrder],
    book: SleeveBook | None,
    handoff_active: bool,
    now: datetime,
) -> tuple[SymbolView, int, Decimal]:
    """Pure: the symbol's view, the open buy intents and their reserved notional.

    * An **open sell intent** (S2) has an order whose end is not proven, or is
      unexpired with no order yet. One whose orders have all ended is done.
    * An **open buy intent** (I4) is an unexpired strategy buy. What it has not
      yet sent is reserved budget for the others.
    """
    orders_by_intent: dict[UUID, list[IntentChildOrder]] = defaultdict(list)
    for order in orders:
        orders_by_intent[order.intent_id].append(order)

    open_sells: set[UUID] = set()
    open_buys = 0
    reserved = ZERO
    for row in intents:
        own = orders_by_intent.get(row.id, [])
        unexpired = now < row.expires_at
        if row.side == "sell" and row.symbol == intent.symbol:
            if any(not order.resolved for order in own) or (unexpired and not own):
                open_sells.add(row.id)
        if (
            row.side == "buy"
            and row.origin == "strategy"
            and unexpired
            and row.id != intent.intent_id
        ):
            open_buys += 1
            sent = sum(
                (
                    child_notional(order.quantity, int(order.limit_price))
                    for order in own
                ),
                ZERO,
            )
            reserved += max(Decimal(row.max_notional_krw) - sent, ZERO)

    unresolved = bool(unresolved_orders(orders, symbol=intent.symbol))
    holdings: Decimal | None = None
    if book is not None and not book.unproven_orders and not book.unattributed_rungs:
        if intent.execution_mode == "shadow" or not unresolved:
            holdings = next(
                (
                    position.quantity
                    for position in book.positions
                    if position.symbol == intent.symbol
                ),
                ZERO,
            )
    return (
        SymbolView(
            handoff_active=handoff_active,
            unresolved_prior_order=unresolved,
            open_sell_intents=frozenset(open_sells),
            holdings=holdings,
        ),
        open_buys,
        reserved,
    )


class UpbitGatewayStore:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        mark_fn: MarkFn = latest_close_marks,
    ) -> None:
        self._session_factory = session_factory
        self._mark_fn = mark_fn

    # ------------------------------------------------------------------ reads

    async def account_of(self, intent_id: UUID) -> str | None:
        """The stored intent's account, or ``None``. Takes no lock."""
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            account = await session.scalar(
                select(ExecutionIntent.account).where(ExecutionIntent.id == intent_id)
            )
            await session.rollback()
            return account

    async def read_intent(self, intent_id: UUID) -> IntentSnapshot | None:
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            row = await session.get(ExecutionIntent, intent_id)
            snapshot = None if row is None else _intent_snapshot(row)
            await session.rollback()
            return snapshot

    async def read_intent_orders(
        self, intent: IntentSnapshot
    ) -> list[IntentChildOrder]:
        """This intent's orders with their evidence. Takes no lock, writes nothing."""
        if intent.sleeve_id is None:
            return []
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            orders = [
                order
                for order in await load_intent_child_orders(session, intent.sleeve_id)
                if order.intent_id == intent.intent_id
            ]
            await session.rollback()
            return orders

    async def list_open_intents(
        self, account: str, *, now: datetime
    ) -> list[dict[str, Any]]:
        """Unexpired intents of one account, oldest first (design §5 R12)."""
        if account not in UPBIT_ACCOUNTS:
            return []
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = await session.scalars(
                select(ExecutionIntent)
                .where(
                    ExecutionIntent.account == account,
                    ExecutionIntent.expires_at > now,
                )
                .order_by(ExecutionIntent.created_at, ExecutionIntent.id)
            )
            listed = [intent_json(_intent_snapshot(row)) for row in rows]
            await session.rollback()
            return listed

    async def shadow_positions(self, sleeve_id: int) -> list[dict[str, str]]:
        """The sleeve's virtual position from its shadowed children (§3.6).

        Reads the journal only. The entry price is the cost of the FIFO lots
        that are still held, divided by their quantity (B18).
        """
        async with self._session_factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            orders = await load_intent_child_orders(session, sleeve_id)
            await session.rollback()
        book_orders, fills = shadow_book_inputs(orders)
        book = compute_sleeve_book(orders=book_orders, fills=fills, marks={})
        return [
            {
                "symbol": position.symbol,
                "quantity": format(position.quantity, "f"),
                "entry_price": format(position.cost_basis / position.quantity, "f"),
            }
            for position in book.positions
        ]

    # ------------------------------------------------------------------ facts

    async def _measure_sleeve(
        self,
        session: AsyncSession,
        *,
        intent: IntentSnapshot,
        sleeve: StrategySleeve,
        orders: Sequence[IntentChildOrder],
        now: datetime,
        exclude_child_id: int | None = None,
    ) -> SleeveBook:
        if intent.execution_mode == "shadow":
            book_orders, fills = shadow_book_inputs(orders)
            unattributed: tuple[int, ...] = ()
        else:
            inputs = await load_sleeve_inputs(session, sleeve)
            book_orders, fills = list(inputs.orders), list(inputs.fills)
            unattributed = inputs.unattributed_rungs
            if exclude_child_id is not None:
                # The pre-send recheck: the child about to be sent is not yet
                # an order, so it must not make its own sleeve unmeasured.
                excluded = child_order_key(exclude_child_id)
                book_orders = [o for o in book_orders if o.rung_id != excluded]
        unmarked = compute_sleeve_book(orders=book_orders, fills=fills, marks={})
        held = [position.symbol for position in unmarked.positions]
        marks = await self._mark_fn(session, sleeve.market, held, now) if held else {}
        marks = {
            sleeve_symbol(key, sleeve.market): value for key, value in marks.items()
        }
        return compute_sleeve_book(
            orders=book_orders,
            fills=fills,
            marks=marks,
            unattributed_rungs=unattributed,
        )

    async def _load_facts(
        self,
        session: AsyncSession,
        intent: IntentSnapshot,
        *,
        now: datetime,
        exclude_child_id: int | None = None,
    ) -> UpbitFacts:
        """Everything a rule reads about the intent's mandate, sleeve and symbol.

        The caller holds the sleeve lock and the mandate row lock when it is
        about to write. A read that fails leaves that part unknown, and unknown
        refuses (``sleeve_unreadable``, ``sleeve_holdings_unmeasured``).
        """
        mandate = None
        if intent.mandate_id is not None:
            row = await session.get(ExecutionMandate, intent.mandate_id)
            if row is not None:
                mandate = mandate_view(
                    row, revoked=await is_revoked(session, intent.mandate_id)
                )
        operator_exit = None
        if intent.operator_exit_id is not None:
            exit_row = await session.get(ExecutionOperatorExit, intent.operator_exit_id)
            if exit_row is not None:
                operator_exit = operator_exit_view(exit_row)

        sleeve_id = intent.sleeve_id
        version = None
        book: SleeveBook | None = None
        symbol = SymbolView(
            handoff_active=True,
            unresolved_prior_order=True,
            open_sell_intents=frozenset(),
            holdings=None,
        )
        open_buys = 0
        reserved = ZERO
        if sleeve_id is None:
            return UpbitFacts(
                mandate, operator_exit, SleeveView(None, None), symbol, 0, now
            )
        try:
            async with session.begin_nested():
                sleeve = await session.get(StrategySleeve, sleeve_id)
                version_row = await current_version(session, sleeve_id)
                if sleeve is None or version_row is None:
                    raise LookupError("sleeve or its version is missing")
                if getattr(sleeve, "execution_path", "card") != "go":
                    raise LookupError("sleeve is not on the go path")
                version = version_view(version_row)
                handoff = await active_handoff(
                    session, sleeve_id=sleeve_id, symbol=intent.symbol
                )
                orders = [
                    order
                    for order in await load_intent_child_orders(session, sleeve_id)
                    if order.child_id != exclude_child_id
                ]
                intents = list(
                    (
                        await session.scalars(
                            select(ExecutionIntent).where(
                                ExecutionIntent.sleeve_id == sleeve_id
                            )
                        )
                    ).all()
                )
                try:
                    async with session.begin_nested():
                        book = await self._measure_sleeve(
                            session,
                            intent=intent,
                            sleeve=sleeve,
                            orders=orders,
                            now=now,
                            exclude_child_id=exclude_child_id,
                        )
                except Exception as exc:  # noqa: BLE001 - an unreadable book refuses
                    logger.warning(
                        "intent_gateway.upbit_book_unreadable sleeve_id=%s error_class=%s",
                        sleeve_id,
                        type(exc).__name__,
                    )
                    book = None
                symbol, open_buys, reserved = sleeve_symbol_views(
                    intent=intent,
                    intents=intents,
                    orders=orders,
                    book=book,
                    handoff_active=handoff is not None,
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001 - an unreadable sleeve refuses
            logger.warning(
                "intent_gateway.upbit_sleeve_unreadable sleeve_id=%s error_class=%s",
                sleeve_id,
                type(exc).__name__,
            )
            version = None
            book = None
        return UpbitFacts(
            mandate=mandate,
            operator_exit=operator_exit,
            sleeve=SleeveView(
                version=version, book=book, reserved_buy_notional=reserved
            ),
            symbol=symbol,
            open_buy_intents=open_buys,
            now=now,
        )

    async def _lock_scope(
        self, session: AsyncSession, *, sleeve_id: int | None, mandate_id: UUID | None
    ) -> None:
        """Sleeve lock, then the mandate row lock (the revocation fence, B10)."""
        if sleeve_id is not None:
            await acquire_sleeve_lock(session, sleeve_id)
        if mandate_id is not None:
            await lock_mandate(session, mandate_id)

    async def facts_before_send(
        self, intent_id: UUID, *, now: datetime, exclude_child_id: int | None = None
    ) -> tuple[IntentSnapshot, UpbitFacts] | None:
        """The facts again, immediately before the POST (design §4.4).

        Reads without locks: it must never wait behind another reservation while
        the broker request is about to go. A revocation that commits after this
        read is the gap B10 states.
        """
        async with self._session_factory() as session:
            row = await session.get(ExecutionIntent, intent_id)
            if row is None:
                await session.rollback()
                return None
            intent = _intent_snapshot(row)
            facts = await self._load_facts(
                session, intent, now=now, exclude_child_id=exclude_child_id
            )
            await session.rollback()
            return intent, facts

    # ------------------------------------------------------------------ writes

    async def store_intent(
        self, intent: ExecutionIntentV2, *, now: datetime, reads: AccountReads
    ) -> None:
        """Insert an authorized v2 intent. The same contents again is a no-op.

        Raises :class:`IntentRefused` when a rule of design §2 refuses it,
        ``IntentNotStorable`` for an expiry outside the window and
        ``IntentConflict`` for a different intent under the same id.
        """
        if intent.expires_at <= now or intent.expires_at - now > MAX_INTENT_LIFETIME:
            raise IntentNotStorable("intent expiry is outside the allowed window")
        digest = contract_digest(intent)
        snapshot = intent_snapshot_from_contract(intent, created_at=now)
        inventory = intent.inventory
        async with self._session_factory() as session:
            await self._lock_scope(
                session, sleeve_id=intent.sleeve_id, mandate_id=intent.mandate_id
            )
            stored = await session.scalar(
                select(ExecutionIntent.intent_digest).where(
                    ExecutionIntent.id == intent.intent_id
                )
            )
            if stored is not None:
                await session.rollback()
                if stored != digest:
                    raise IntentConflict("intent id is stored with different contents")
                return
            facts = await self._load_facts(session, snapshot, now=now)
            decision = check_storable(snapshot, facts=facts, reads=reads)
            if decision.refusal is not None:
                if decision.stop_sleeve is not None:
                    await self._stop_sleeve(session, decision, intent.intent_id)
                    await session.commit()
                else:
                    await session.rollback()
                raise IntentRefused(decision.refusal)
            await session.execute(
                insert(ExecutionIntent).values(
                    id=intent.intent_id,
                    schema_version=intent.schema_version,
                    strategy=intent.strategy,
                    account=intent.account,
                    venue=intent.venue,
                    symbol=intent.symbol,
                    side=intent.side,
                    max_notional_krw=intent.max_notional_krw,
                    limit_price_krw=intent.limit_price_krw,
                    max_slippage_bps=intent.max_slippage_bps,
                    policy_version=intent.policy_version,
                    model_version=intent.model_version,
                    authorization_ref=intent.authorization_ref,
                    expires_at=intent.expires_at,
                    intent_digest=digest,
                    mandate_id=intent.mandate_id,
                    sleeve_id=intent.sleeve_id,
                    execution_mode=intent.execution_mode,
                    strategy_code_sha=intent.strategy_code_sha,
                    decision_ref=intent.decision_ref,
                    origin=intent.origin,
                    operator_exit_id=intent.operator_exit_id,
                    max_quantity_decimal=Decimal(intent.max_quantity_decimal),
                    position_at_authorization_decimal=Decimal(
                        inventory.position_at_authorization
                    ),
                    min_position_after_decimal=Decimal(inventory.min_position_after),
                    max_position_after_decimal=Decimal(inventory.max_position_after),
                )
            )
            await session.commit()

    async def _stop_sleeve(
        self, session: AsyncSession, decision: UpbitDecision, intent_id: UUID
    ) -> None:
        if decision.stop_sleeve is None:
            return
        try:
            async with session.begin_nested():
                await stop_at_loss_limit(
                    session,
                    expected=decision.stop_sleeve,
                    reason=f"loss reached the loss limit at intent {intent_id}",
                )
        except Exception as exc:  # noqa: BLE001 - the refusal already holds
            logger.error(
                "intent_gateway.upbit_loss_stop_write_failed error_class=%s",
                type(exc).__name__,
            )

    async def _child_by_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> ChildSnapshot | None:
        row = await session.scalar(
            select(ExecutionChildRequest).where(
                ExecutionChildRequest.idempotency_key == idempotency_key
            )
        )
        return None if row is None else _child_snapshot(row)

    async def reserve(
        self,
        request: AnyChild,
        *,
        request_digest: str,
        decide: UpbitDecide,
        now_fn: Callable[[], datetime],
    ) -> UpbitReservation:
        key = request.idempotency_key
        async with self._session_factory() as session:
            scope = (
                await session.execute(
                    select(ExecutionIntent.sleeve_id, ExecutionIntent.mandate_id).where(
                        ExecutionIntent.id == request.intent_id
                    )
                )
            ).first()
            if scope is None:
                existing = await self._child_by_key(session, key)
                await session.rollback()
                if existing is not None:
                    return _replay_or_conflict(existing, request_digest)
                return UpbitReservation(refusal_without_row=Refusal.INTENT_NOT_FOUND)
            await self._lock_scope(session, sleeve_id=scope[0], mandate_id=scope[1])
            intent_row = await session.scalar(
                select(ExecutionIntent)
                .where(ExecutionIntent.id == request.intent_id)
                .with_for_update()
            )
            if intent_row is None:  # pragma: no cover - intents are never deleted
                await session.rollback()
                return UpbitReservation(refusal_without_row=Refusal.INTENT_NOT_FOUND)
            intent = _intent_snapshot(intent_row)
            existing = await self._child_by_key(session, key)
            if existing is not None:
                await session.rollback()
                return _replay_or_conflict(existing, request_digest)

            rows = await session.scalars(
                select(ExecutionChildRequest)
                .where(ExecutionChildRequest.intent_id == request.intent_id)
                .order_by(ExecutionChildRequest.id)
            )
            children = [_child_snapshot(row) for row in rows]
            # The clock is read under the locks: a wait for them must not let an
            # expired authorization pass on a stale time.
            now = now_fn()
            facts = await self._load_facts(session, intent, now=now)
            decision = decide(intent, children, facts)
            if not decision.journal:
                await session.rollback()
                return UpbitReservation(
                    intent=intent,
                    decision=decision,
                    refusal_without_row=decision.refusal,
                )
            refused = decision.refusal is not None
            quantity = getattr(request, "quantity_decimal", None)
            row = ExecutionChildRequest(
                intent_id=request.intent_id,
                idempotency_key=key,
                request_digest=request_digest,
                action=request.action,
                target_idempotency_key=getattr(request, "target_idempotency_key", None),
                quantity=None,
                quantity_decimal=None if quantity is None else Decimal(quantity),
                limit_price_krw=getattr(request, "limit_price_krw", None),
                state=(ChildState.REFUSED if refused else ChildState.SENDING).value,
                refusal_reason=decision.refusal.value if decision.refusal else None,
                finished_at=now if refused else None,
            )
            session.add(row)
            try:
                await session.flush()
                reserved = _child_snapshot(row)
                if (
                    decision.open_handoff
                    and intent.sleeve_id is not None
                    and intent.mandate_id is not None
                    and decision.refusal is not None
                ):
                    # S5 (a): atomic with the refused row. If this fails the row
                    # is not journaled either, and the retry decides again.
                    await open_exit_handoff(
                        session,
                        sleeve_id=intent.sleeve_id,
                        symbol=intent.symbol,
                        mandate_id=intent.mandate_id,
                        reason=decision.refusal.value,
                        opened_by=GATEWAY_HANDOFF_ACTOR,
                    )
                await self._stop_sleeve(session, decision, intent.intent_id)
                await session.commit()
            except IntegrityError:
                # The key was taken under a different intent, whose locks this
                # transaction does not hold.
                await session.rollback()
                existing = await self._child_by_key(session, key)
                await session.rollback()
                if existing is None:
                    raise
                return _replay_or_conflict(existing, request_digest)
            return UpbitReservation(
                intent=intent, child=reserved, is_new=True, decision=decision
            )

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
        refusal_reason: str | None = None,
    ) -> bool:
        """Record the outcome once; a row that left ``sending`` is never rewritten.

        ``refusal_reason`` is set only for ``not_sent`` (B29), and the journal
        trigger refuses any other use of it.
        """
        values: dict[str, Any] = {
            "state": state.value,
            "broker_order_id": broker_order_id,
            "result": result,
            "finished_at": now,
        }
        if state is ChildState.NOT_SENT:
            values["refusal_reason"] = refusal_reason
        async with self._session_factory() as session:
            outcome = await session.execute(
                update(ExecutionChildRequest)
                .where(
                    ExecutionChildRequest.id == child_id,
                    ExecutionChildRequest.state == ChildState.SENDING.value,
                )
                .values(**values)
            )
            await session.commit()
        return (outcome.rowcount or 0) == 1


def intent_json(intent: IntentSnapshot) -> dict[str, Any]:
    """One open intent as the executor reads it. No account number, no mandate terms."""

    def exact(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    return {
        "intent_id": str(intent.intent_id),
        "schema_version": intent.schema_version,
        "account": intent.account,
        "venue": intent.venue,
        "execution_mode": intent.execution_mode,
        "strategy": intent.strategy,
        "symbol": intent.symbol,
        "side": intent.side,
        "max_quantity_decimal": exact(intent.max_quantity_decimal),
        "max_notional_krw": intent.max_notional_krw,
        "limit_price_krw": intent.limit_price_krw,
        "max_slippage_bps": intent.max_slippage_bps,
        "expires_at": intent.expires_at.isoformat(),
        "inventory": {
            "position_at_authorization": exact(
                intent.position_at_authorization_decimal
            ),
            "min_position_after": exact(intent.min_position_after_decimal),
            "max_position_after": exact(intent.max_position_after_decimal),
        },
        "origin": intent.origin,
        "decision_ref": intent.decision_ref,
    }
