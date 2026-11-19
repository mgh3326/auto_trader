"""Orders a ``go`` sleeve placed through execution intents (hk 5441).

A ``go`` sleeve (design 5438 S1, S7) trades through intent children, not through
proposal rungs. This module reads those children as orders, for the two readers
that need them:

* the sleeve book (the second attribution source, design §1.4): a live child's
  fills are the in-effect ``execution_ledger`` rows that carry its Upbit order
  uuid;
* the checks that need an order's end proven before anything else may happen on
  its symbol (B5, S2, S5 c).

An order is **resolved** only on positive evidence that the broker closed it and
how much of it traded: a ``review.execution_child_order_evidence`` row whose
``broker_state`` is final. A missing row, a failed lookup or a still-open order
is unresolved. Nothing here guesses, and nothing here writes.

A shadow child never reached a broker. It counts as filled in full at its limit
price when the journal recorded it (design §3.6), for the shadow book only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.models.execution_mandates import ExecutionChildOrderEvidence
from app.services.strategy_sleeves.book import SleeveFill, SleeveOrder

ZERO = Decimal("0")
#: Upbit order states after which the order can no longer trade.
FINAL_BROKER_STATES = frozenset({"done", "cancel"})
#: Journal states of a child that is, or may be, an order at the broker.
_LIVE_ORDER_STATES = frozenset({"sending", "accepted", "uncertain"})
_SHADOW_ORDER_STATE = "shadowed"


def child_order_key(child_id: int) -> int:
    """The book key of a child order.

    The book keys orders by an integer that is a rung id for a card order. A
    child order uses the negative of its journal id, so the two never collide.
    """
    return -int(child_id)


@dataclass(frozen=True)
class ChildOrderEvidence:
    order_uuid: str
    broker_state: str
    executed_volume: Decimal
    remaining_volume: Decimal | None
    observed_at: datetime


@dataclass(frozen=True)
class IntentChildOrder:
    """One submit child of a sleeve's intent, with what is known about its order."""

    child_id: int
    intent_id: UUID
    idempotency_key: str
    symbol: str
    side: str
    execution_mode: str
    decision_ref: str | None
    state: str
    quantity: Decimal
    limit_price: Decimal
    broker_order_id: str | None
    intent_created_at: datetime
    created_at: datetime
    finished_at: datetime | None
    evidence: ChildOrderEvidence | None = None

    @property
    def is_shadow(self) -> bool:
        """A child of a shadow intent. The mode is immutable and decides this.

        Its journal state does not: a shadow child whose ``shadowed`` outcome
        was not recorded yet is still ``sending``, and it never reached a broker
        either. It is counted as the assumed fill it is about to become, so its
        notional is never missing from both the reservation and the book.
        """
        return self.execution_mode == "shadow"

    @property
    def shadow_recorded(self) -> bool:
        """A shadow child whose ``shadowed`` outcome is in the journal.

        The book counts a shadow child from its reservation on. A reader that
        reports what was *recorded* -- the intent query -- asks this instead:
        a shadow child still ``sending`` has not been answered yet.
        """
        return self.is_shadow and self.state == _SHADOW_ORDER_STATE

    @property
    def order_uuid(self) -> str | None:
        """The Upbit order this child is bound to, when exactly one is named.

        The journal names it when the outcome was recorded. When it was not
        (B3), the evidence row found under the child's own identifier names it.
        Two different names bind nothing.
        """
        journal = (self.broker_order_id or "").strip() or None
        found = self.evidence.order_uuid.strip() if self.evidence else None
        if journal and found and journal != found:
            return None
        return journal or found or None

    @property
    def resolved(self) -> bool:
        if self.is_shadow:
            return True
        return (
            self.evidence is not None
            and self.order_uuid is not None
            and self.evidence.broker_state in FINAL_BROKER_STATES
        )

    @property
    def filled_quantity(self) -> Decimal | None:
        """Proven traded quantity; ``None`` while the order is unresolved."""
        if self.is_shadow:
            return self.quantity
        if not self.resolved or self.evidence is None:
            return None
        return self.evidence.executed_volume

    def as_sleeve_order(self) -> SleeveOrder:
        """The order in the sleeve book's terms."""
        filled = self.filled_quantity
        if filled is not None:
            state = "filled" if filled >= self.quantity else "cancelled"
        elif self.order_uuid is not None:
            state = "resting"
        else:
            # Maybe at the broker, under an order nobody can name yet (B3).
            state = "unverified"
        return SleeveOrder(
            proposal_pk=0,
            rung_id=child_order_key(self.child_id),
            symbol=self.symbol,
            side=self.side,
            state=state,
            quantity=self.quantity,
            limit_price=self.limit_price,
            notional=None,
            rung_filled_qty=filled,
            signal_key=self.decision_ref,
            signal_at=self.intent_created_at,
            fill_unproven=filled is None and self.order_uuid is None,
        )


def live_orders(orders: Iterable[IntentChildOrder]) -> list[IntentChildOrder]:
    return [order for order in orders if not order.is_shadow]


def unresolved_orders(
    orders: Iterable[IntentChildOrder], *, symbol: str
) -> list[IntentChildOrder]:
    """Live orders on ``symbol`` whose end the broker has not proven."""
    return [
        order
        for order in orders
        if order.symbol == symbol and not order.is_shadow and not order.resolved
    ]


def shadow_book_inputs(
    orders: Iterable[IntentChildOrder],
) -> tuple[list[SleeveOrder], list[SleeveFill]]:
    """Shadow children as filled orders and their virtual fills (design §3.6).

    The caller folds these with ``compute_sleeve_book``, so the shadow position
    and its cost are the same FIFO lots a live sleeve would hold (B18).
    """
    book_orders: list[SleeveOrder] = []
    fills: list[SleeveFill] = []
    for order in orders:
        if not order.is_shadow:
            continue
        book_orders.append(order.as_sleeve_order())
        fills.append(
            SleeveFill(
                ledger_id=order.child_id,
                rung_id=child_order_key(order.child_id),
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                price=order.limit_price,
                fee=ZERO,
                # The reservation time, never the time the outcome was
                # recorded: it is committed before anything can depend on the
                # fill and does not move when a row left ``sending`` is
                # finished later, so the FIFO order of virtual fills is fixed.
                filled_at=order.created_at,
            )
        )
    return book_orders, fills


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    parsed = Decimal(str(value))
    return parsed if parsed.is_finite() else None


def build_child_orders(
    rows: Sequence[tuple[ExecutionIntent, ExecutionChildRequest]],
    evidence_rows: Iterable[ExecutionChildOrderEvidence],
) -> list[IntentChildOrder]:
    """Pure: journal rows and evidence rows to orders, in journal order."""
    evidence_by_child: dict[int, ChildOrderEvidence] = {}
    for row in evidence_rows:
        executed = _decimal(row.executed_volume)
        if executed is None:
            continue
        evidence_by_child[row.child_id] = ChildOrderEvidence(
            order_uuid=str(row.order_uuid),
            broker_state=str(row.broker_state),
            executed_volume=executed,
            remaining_volume=_decimal(row.remaining_volume),
            observed_at=row.observed_at,
        )
    orders: list[IntentChildOrder] = []
    for intent, child in rows:
        if child.action != "submit":
            continue
        if child.state not in _LIVE_ORDER_STATES and child.state != _SHADOW_ORDER_STATE:
            continue
        quantity = _decimal(child.quantity_decimal)
        price = _decimal(child.limit_price_krw)
        if quantity is None or price is None:
            raise ValueError("a journaled upbit submit has no quantity or price")
        orders.append(
            IntentChildOrder(
                child_id=child.id,
                intent_id=intent.id,
                idempotency_key=child.idempotency_key,
                symbol=intent.symbol,
                side=intent.side,
                execution_mode=str(intent.execution_mode),
                decision_ref=intent.decision_ref,
                state=child.state,
                quantity=quantity,
                limit_price=price,
                broker_order_id=child.broker_order_id,
                intent_created_at=intent.created_at,
                created_at=child.created_at,
                finished_at=child.finished_at,
                evidence=evidence_by_child.get(child.id),
            )
        )
    return orders


async def load_intent_child_orders(
    session: AsyncSession, sleeve_id: int
) -> list[IntentChildOrder]:
    """Every order the sleeve's intents placed, shadow and live. Reads only."""
    result = await session.execute(
        select(ExecutionIntent, ExecutionChildRequest)
        .join(
            ExecutionChildRequest,
            ExecutionChildRequest.intent_id == ExecutionIntent.id,
        )
        .where(ExecutionIntent.sleeve_id == sleeve_id)
        .order_by(ExecutionChildRequest.id)
    )
    rows = [(intent, child) for intent, child in result.all()]
    child_ids = [child.id for _intent, child in rows]
    evidence: list[ExecutionChildOrderEvidence] = []
    if child_ids:
        found = await session.execute(
            select(ExecutionChildOrderEvidence).where(
                ExecutionChildOrderEvidence.child_id.in_(child_ids)
            )
        )
        evidence = list(found.scalars().all())
    return build_child_orders(rows, evidence)
