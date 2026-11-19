"""Exit handoffs: a symbol handed from the Go path to an approval card (hk 5441).

Design ``docs/design/5438-go-execution-path.md`` S5, O1-O4. The loss-sell guard
is not changed, so a sale below average buy price x 1.01 can only be a card.
This module is the only writer of ``review.execution_exit_handoffs`` and the one
narrow door through which a card may tag a ``go`` sleeve.

States (a DB trigger refuses any other move)::

    open -> ready_for_card -> card_claimed -> closed
                    ^               |
                    +---------------+   (card ended unsent, or a remainder is left)

* ``open``: opened under the sleeve lock when the gateway refuses a sell as
  ``loss_sell_requires_card``, or by the strategy job for an exit it expects to
  be below the floor. While a handoff is not ``closed`` the gateway refuses every
  new intent and every submit on that sleeve and symbol; cancels pass.
* ``ready_for_card``: every order on the symbol has positive evidence of its end
  and its traded quantity (:func:`advance_exit_handoff`). An unresolved order
  keeps the handoff ``open``.
* ``card_claimed``: one card holds the claim (:func:`claim_exit_handoff`). The
  claim is a conditional UPDATE, so a second card finds no row to claim.
* back to ``ready_for_card`` or to ``closed``: only :func:`settle_exit_handoff`
  decides, on evidence.

**The send fence (O1, O3).** Before a claimed card may send,
:func:`fence_card_send` commits ``send_attempts + 1`` in its own transaction,
under the sleeve lock, conditional on the handoff still being claimed by that
card **and on every earlier attempt being proven unsent**. The count therefore
survives a rollback of the approval that raised it, which the proposal row does
not: after an approval core that failed with an unknown outcome the proposal can
read ``pending_approval`` with a valid nonce although the broker was reached. A
second click on that card reaches the fence and is refused, so one card posts at
most once until somebody has looked. A card is *proven unsent* only when the
proposal ended in a no-send state under its row lock, its nonce is dead, no rung
ever got a broker id, **and** every counted attempt was proven not to have
posted. A W5 pre-core dead letter changes nothing on the proposal, so it proves
nothing here either: the handoff stays claimed, and no second card can be
created while the first can still be approved.

The same committed UPDATE sets ``fills_unmeasured`` (O4): from the moment a card
may have posted, the whole sleeve is unmeasured. Only a proven non-send
(:func:`record_card_proven_unsent`) or a settle that found the broker and the
ledger equal clears it.

**The fill proof (O2, O4).** A card that may have sent is settled only when the
broker's order reading is final and its cumulative executed volume equals the
ledger fills attributed to the card's rung. If either cannot be read, or they
differ, the handoff stays claimed and ``fills_unmeasured`` is set. The sleeve
book then reports the whole sleeve as unmeasured, so no buy on any symbol of the
sleeve passes its gate until a later settle proves the two equal.

The card's account is checked four times against the mandate's ``account_ref``
(B12): at the claim, at dispatch, at the approval's revalidation (the fence) and
immediately before the POST.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_mandates import ExecutionExitHandoff, ExecutionMandate
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.strategy_sleeves import StrategySleeve
from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.intent_orders import (
    FINAL_BROKER_STATES,
    load_intent_child_orders,
    unresolved_orders,
)
from app.services.strategy_sleeves.store import (
    acquire_sleeve_lock,
    load_sleeve_inputs,
    sleeve_symbol,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
ACTIVE_STATES: Final = ("open", "ready_for_card", "card_claimed")
#: Proposal lifecycle states that end a card without any approval completing.
_NO_SEND_GROUP_STATES: Final = frozenset(
    {"expired", "rejected", "voided", "superseded"}
)
#: Rung states a rung can reach without ever having been at the broker.
_NO_SEND_RUNG_STATES: Final = frozenset({"expired", "rejected", "voided", "superseded"})


class ExitHandoffError(ValueError):
    """A handoff step that is refused. Nothing was written.

    ``code`` is a closed reason a caller can show or store.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SettleOutcome(StrEnum):
    NOT_CLAIMED = "not_claimed"
    #: The card is busy, still approvable, or ended in a state that proves nothing.
    HELD = "held"
    #: The card may have sent and its fills are not proven (O2, O4).
    HELD_UNMEASURED = "held_unmeasured"
    RETURNED_UNSENT = "returned_unsent"
    RETURNED_REMAINDER = "returned_remainder"
    CLOSED = "closed"


@dataclass(frozen=True)
class BrokerOrderReading:
    """What the broker said about the card's order, read by the caller.

    ``order_uuid`` is ``None`` when the order was not found or the read failed;
    neither is evidence that no order exists.
    """

    order_uuid: str | None
    state: str | None
    executed_volume: Decimal | None

    @property
    def final(self) -> bool:
        return (
            self.order_uuid is not None
            and self.state in FINAL_BROKER_STATES
            and self.executed_volume is not None
            and self.executed_volume.is_finite()
            and self.executed_volume >= 0
        )


UNREADABLE_ORDER: Final = BrokerOrderReading(None, None, None)


@dataclass(frozen=True)
class CardPlan:
    """What a settle needs to read from the broker before it takes its locks."""

    handoff_id: int
    claim_count: int
    proposal_id: uuid.UUID
    may_have_sent: bool
    order_uuid: str | None
    client_identifier: str | None


@dataclass(frozen=True)
class SettleResult:
    outcome: SettleOutcome
    state: str
    detail: str | None = None


async def active_handoff(
    session: AsyncSession, *, sleeve_id: int, symbol: str
) -> ExecutionExitHandoff | None:
    """The handoff that is not closed for this sleeve and symbol, if any."""
    return await session.scalar(
        select(ExecutionExitHandoff).where(
            ExecutionExitHandoff.sleeve_id == sleeve_id,
            ExecutionExitHandoff.symbol == symbol,
            ExecutionExitHandoff.state.in_(ACTIVE_STATES),
        )
    )


async def open_exit_handoff(
    session: AsyncSession,
    *,
    sleeve_id: int,
    symbol: str,
    mandate_id: uuid.UUID,
    reason: str,
    opened_by: str,
) -> tuple[ExecutionExitHandoff, bool]:
    """Open the handoff for a sleeve and symbol (S5 a). The caller commits.

    The caller holds the sleeve lock. One handoff is active per sleeve and
    symbol: an existing one is returned unchanged (``False``).
    """
    existing = await active_handoff(session, sleeve_id=sleeve_id, symbol=symbol)
    if existing is not None:
        return existing, False
    row = ExecutionExitHandoff(
        sleeve_id=sleeve_id,
        symbol=symbol,
        mandate_id=mandate_id,
        state="open",
        open_reason=reason[:64],
        opened_by=opened_by[:128],
    )
    session.add(row)
    await session.flush()
    return row, True


async def proven_holdings(
    session: AsyncSession, sleeve: StrategySleeve, symbol: str
) -> Decimal | None:
    """The sleeve's own position in ``symbol``, or ``None`` when it is not proven.

    The position is the FIFO fold of ledger-attributed fills. It is unknown when
    any order of the sleeve has unproven fills, a rung could not be attributed,
    or an order on the symbol has no proven end. Unknown is never zero.
    """
    inputs = await load_sleeve_inputs(session, sleeve)
    book = compute_sleeve_book(
        orders=inputs.orders,
        fills=inputs.fills,
        marks={},
        unattributed_rungs=inputs.unattributed_rungs,
    )
    if book.unproven_orders or book.unattributed_rungs:
        return None
    orders = await load_intent_child_orders(session, sleeve.id)
    if unresolved_orders(orders, symbol=symbol):
        return None
    for position in book.positions:
        if position.symbol == symbol:
            return position.quantity
    return ZERO


async def _locked(
    session: AsyncSession, handoff_id: int
) -> ExecutionExitHandoff | None:
    # ``populate_existing``: callers load the row once to learn its sleeve,
    # then wait for the sleeve lock. Without it this SELECT would hand back the
    # attributes read before the wait, and a send fence committed meanwhile
    # would be invisible to the decision made under the lock.
    return await session.scalar(
        select(ExecutionExitHandoff)
        .where(ExecutionExitHandoff.id == handoff_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def advance_exit_handoff(
    session: AsyncSession, handoff_id: int, *, now: datetime
) -> str:
    """``open`` to ``ready_for_card`` or ``closed`` (S5 c). The caller commits.

    Returns the state after the call. Any order on the symbol whose end is not
    proven keeps the handoff ``open``; so does an unproven position.
    """
    handoff = await session.get(ExecutionExitHandoff, handoff_id)
    if handoff is None:
        raise ExitHandoffError("exit_handoff_not_found")
    await acquire_sleeve_lock(session, handoff.sleeve_id)
    handoff = await _locked(session, handoff_id)
    if handoff is None or handoff.state != "open":
        return "missing" if handoff is None else handoff.state
    sleeve = await session.get(StrategySleeve, handoff.sleeve_id)
    if sleeve is None:
        return handoff.state
    holdings = await proven_holdings(session, sleeve, handoff.symbol)
    if holdings is None:
        return handoff.state
    if holdings > 0:
        handoff.state = "ready_for_card"
    else:
        handoff.state = "closed"
        handoff.closed_at = now
    handoff.updated_at = now
    await session.flush()
    return handoff.state


def _account_matches(mandate: ExecutionMandate | None, configured: str | None) -> bool:
    return bool(
        mandate is not None
        and mandate.account_ref
        and configured
        and mandate.account_ref == configured
    )


async def claim_exit_handoff(
    session: AsyncSession,
    *,
    handoff_id: int,
    proposal_id: uuid.UUID,
    sleeve_id: int,
    symbol: str,
    side: str,
    rung_quantities: list[Decimal],
    configured_account_ref: str | None,
    now: datetime,
) -> ExecutionExitHandoff:
    """Claim a ``ready_for_card`` handoff for one new card (S5 d).

    Runs in the transaction that inserts the proposal, before the insert. The
    claim is the conditional UPDATE: a concurrent second card changes no row and
    is refused. The reserved quantity is the position proven at this moment and
    stays reserved until the card is settled.
    """
    handoff = await session.get(ExecutionExitHandoff, handoff_id)
    if handoff is None or handoff.sleeve_id != sleeve_id:
        raise ExitHandoffError("exit_handoff_not_found")
    await acquire_sleeve_lock(session, sleeve_id)
    handoff = await _locked(session, handoff_id)
    sleeve = await session.get(StrategySleeve, sleeve_id)
    if handoff is None or sleeve is None:
        raise ExitHandoffError("exit_handoff_not_found")
    if sleeve_symbol(symbol, sleeve.market) != handoff.symbol:
        raise ExitHandoffError("exit_handoff_symbol_mismatch")
    if side != "sell" or len(rung_quantities) != 1:
        raise ExitHandoffError("exit_handoff_card_shape")
    quantity = rung_quantities[0]
    if not quantity.is_finite() or quantity <= 0:
        raise ExitHandoffError("exit_handoff_card_shape")
    if handoff.state != "ready_for_card":
        raise ExitHandoffError("exit_handoff_not_ready")
    mandate = await session.get(ExecutionMandate, handoff.mandate_id)
    if not _account_matches(mandate, configured_account_ref):
        raise ExitHandoffError("exit_handoff_account_mismatch")
    holdings = await proven_holdings(session, sleeve, handoff.symbol)
    if holdings is None:
        raise ExitHandoffError("exit_handoff_holdings_unmeasured")
    if holdings <= 0 or quantity > holdings:
        raise ExitHandoffError("exit_handoff_quantity_exceeds_holdings")
    claimed = await session.execute(
        update(ExecutionExitHandoff)
        .where(
            ExecutionExitHandoff.id == handoff_id,
            ExecutionExitHandoff.state == "ready_for_card",
        )
        .values(
            state="card_claimed",
            proposal_id=proposal_id,
            reserved_quantity=holdings,
            claim_count=ExecutionExitHandoff.claim_count + 1,
            send_attempts=0,
            send_attempts_proven_unsent=0,
            fills_unmeasured=False,
            updated_at=now,
        )
    )
    if (claimed.rowcount or 0) != 1:
        raise ExitHandoffError("exit_handoff_not_ready")
    await session.refresh(handoff)
    return handoff


async def card_refusal(
    session: AsyncSession,
    *,
    handoff_id: int,
    proposal_id: uuid.UUID,
    configured_account_ref: str | None,
) -> str | None:
    """Why this card may not go on, or ``None``. Reads only.

    Used at dispatch and immediately before the POST: the card must still hold
    the claim and the configured account must still be the mandate's.
    """
    handoff = await session.get(ExecutionExitHandoff, handoff_id)
    if (
        handoff is None
        or handoff.state != "card_claimed"
        or handoff.proposal_id != proposal_id
    ):
        return "exit_handoff_claim_lost"
    mandate = await session.get(ExecutionMandate, handoff.mandate_id)
    if not _account_matches(mandate, configured_account_ref):
        return "exit_handoff_account_mismatch"
    return None


async def fence_card_send(
    session_factory: Callable[[], AsyncSession],
    *,
    handoff_id: int,
    proposal_id: uuid.UUID,
    configured_account_ref: str | None,
    now: datetime,
) -> None:
    """Count a send attempt in its own committed transaction, or refuse.

    Call this before the card's rung may reach the broker. It raises unless the
    handoff is still claimed by this card, the account matches, and no earlier
    attempt of this card is still unexplained (O1, O3: an approval that reached
    the broker and then rolled back leaves the card clickable; the count it
    committed is what stops the second POST). If the commit itself fails the
    caller must not send: the count is what later tells a possibly-sent card
    from a proven-unsent one.

    The same UPDATE marks the sleeve unmeasured (O4). It runs under the sleeve
    lock, so it is ordered against every reservation on the sleeve: a buy
    reserved after it sees the mark, and one reserved before it reads the mark
    again in its pre-send check.
    """
    async with session_factory() as session:
        first = await session.get(ExecutionExitHandoff, handoff_id)
        if first is None:
            await session.rollback()
            raise ExitHandoffError("exit_handoff_claim_lost")
        # The approval that calls this holds no sleeve lock. Should that ever
        # change, waiting here would never end, so the wait is bounded: no lock,
        # no count, no send.
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        await acquire_sleeve_lock(session, first.sleeve_id)
        handoff = await _locked(session, handoff_id)
        if (
            handoff is None
            or handoff.state != "card_claimed"
            or handoff.proposal_id != proposal_id
        ):
            await session.rollback()
            raise ExitHandoffError("exit_handoff_claim_lost")
        if handoff.send_attempts != handoff.send_attempts_proven_unsent:
            await session.rollback()
            raise ExitHandoffError("exit_handoff_prior_attempt_unresolved")
        mandate = await session.get(ExecutionMandate, handoff.mandate_id)
        if not _account_matches(mandate, configured_account_ref):
            await session.rollback()
            raise ExitHandoffError("exit_handoff_account_mismatch")
        counted = await session.execute(
            update(ExecutionExitHandoff)
            .where(
                ExecutionExitHandoff.id == handoff_id,
                ExecutionExitHandoff.state == "card_claimed",
                ExecutionExitHandoff.proposal_id == proposal_id,
                ExecutionExitHandoff.send_attempts
                == ExecutionExitHandoff.send_attempts_proven_unsent,
            )
            .values(
                send_attempts=ExecutionExitHandoff.send_attempts + 1,
                fills_unmeasured=True,
                updated_at=now,
            )
        )
        if (counted.rowcount or 0) != 1:
            await session.rollback()
            raise ExitHandoffError("exit_handoff_claim_lost")
        await session.commit()


async def record_card_proven_unsent(
    session_factory: Callable[[], AsyncSession],
    *,
    handoff_id: int,
    proposal_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Record that the last counted attempt provably did not post.

    Only the pre-send hook raising before the request proves that. A failure to
    record leaves the attempt counted as possibly sent, which is the safe side.
    With every attempt explained again, the mark the fence set is cleared.
    """
    try:
        async with session_factory() as session:
            recorded = await session.execute(
                update(ExecutionExitHandoff)
                .where(
                    ExecutionExitHandoff.id == handoff_id,
                    ExecutionExitHandoff.state == "card_claimed",
                    ExecutionExitHandoff.proposal_id == proposal_id,
                    ExecutionExitHandoff.send_attempts_proven_unsent
                    < ExecutionExitHandoff.send_attempts,
                )
                .values(
                    send_attempts_proven_unsent=(
                        ExecutionExitHandoff.send_attempts_proven_unsent + 1
                    ),
                    fills_unmeasured=(
                        ExecutionExitHandoff.send_attempts_proven_unsent + 1
                        < ExecutionExitHandoff.send_attempts
                    ),
                    updated_at=now,
                )
            )
            await session.commit()
            return (recorded.rowcount or 0) == 1
    except Exception as exc:  # noqa: BLE001 - stays counted as possibly sent
        logger.error(
            "exit_handoff.proven_unsent_not_recorded",
            extra={"handoff_id": handoff_id, "exception_type": type(exc).__name__},
        )
        return False


def card_proven_unsent(handoff: Any, group: Any, rungs: list[Any]) -> bool:
    """O1, O3: whether the claimed card provably never reached the broker. Pure.

    Every condition must hold. The proposal's own state is not enough on its
    own: an approval that failed after the broker call rolls it back to
    ``pending_approval``. The send counter, committed outside that approval,
    closes that gap.
    """
    if handoff.send_attempts != handoff.send_attempts_proven_unsent:
        return False
    if group.lifecycle_state not in _NO_SEND_GROUP_STATES:
        return False
    if group.approval_nonce is not None and group.approval_nonce_used_at is None:
        return False
    if not rungs:
        return False
    # With no attempt ever counted, the rung must look untouched. With counted
    # attempts that the fence all proved unsent, the order layer was entered and
    # stopped before the request: it may have left its idempotency key on the
    # rung, and a rung it left ``unverified`` ends ``voided_local_stale``. The
    # committed counters are the proof there, not the rung's appearance.
    attempted = handoff.send_attempts > 0
    allowed_states = (
        _NO_SEND_RUNG_STATES | {"voided_local_stale"}
        if attempted
        else _NO_SEND_RUNG_STATES
    )
    for rung in rungs:
        filled = rung.filled_qty
        if (
            rung.state not in allowed_states
            or rung.broker_order_id is not None
            or (rung.idempotency_key is not None and not attempted)
            or (filled is not None and Decimal(str(filled)) != 0)
        ):
            return False
    return True


def sent_card_problem(
    *,
    order_uuid: str | None,
    reading: BrokerOrderReading | None,
    ledger_attributed: Decimal | None,
) -> str | None:
    """O2, O4: why a card that may have sent is not proven, or ``None``. Pure.

    Proven means all of: the card's order is named, the broker's reading of that
    order is final, the ledger's attributed total could be read, and the two
    quantities are equal. Anything unread counts as not proven; a missing value
    is never treated as zero.
    """
    if order_uuid is None:
        return "card_order_not_named"
    if reading is None or not reading.final or reading.order_uuid != order_uuid:
        return "broker_reading_unavailable"
    if ledger_attributed is None:
        return "ledger_unreadable"
    if ledger_attributed != reading.executed_volume:
        return "ledger_total_differs_from_broker"
    return None


def _client_identifier(proposal_id: uuid.UUID, rung_index: int) -> str:
    from app.services.order_proposals.revalidation import _proposal_client_order_id

    return str(_proposal_client_order_id(proposal_id, rung_index))


async def plan_card_settle(session: AsyncSession, handoff_id: int) -> CardPlan | None:
    """What to read from the broker before :func:`settle_exit_handoff`. Reads only."""
    handoff = await session.get(ExecutionExitHandoff, handoff_id)
    if (
        handoff is None
        or handoff.state != "card_claimed"
        or handoff.proposal_id is None
    ):
        return None
    rows = await session.execute(
        select(OrderProposalRung)
        .join(OrderProposal, OrderProposal.id == OrderProposalRung.proposal_pk)
        .where(OrderProposal.proposal_id == handoff.proposal_id)
        .order_by(OrderProposalRung.rung_index)
    )
    rungs = list(rows.scalars().all())
    rung = rungs[0] if len(rungs) == 1 else None
    order_uuid = (rung.broker_order_id or "").strip() or None if rung else None
    return CardPlan(
        handoff_id=handoff.id,
        claim_count=handoff.claim_count,
        proposal_id=handoff.proposal_id,
        may_have_sent=handoff.send_attempts != handoff.send_attempts_proven_unsent
        or order_uuid is not None,
        order_uuid=order_uuid,
        client_identifier=(
            _client_identifier(handoff.proposal_id, rung.rung_index) if rung else None
        ),
    )


def _release(handoff: ExecutionExitHandoff, *, state: str, now: datetime) -> None:
    handoff.state = state
    handoff.proposal_id = None
    handoff.reserved_quantity = None
    handoff.fills_unmeasured = False
    handoff.updated_at = now
    if state == "closed":
        handoff.closed_at = now


async def _hold_unmeasured(
    session: AsyncSession, handoff: ExecutionExitHandoff, *, now: datetime, detail: str
) -> SettleResult:
    if not handoff.fills_unmeasured:
        handoff.fills_unmeasured = True
        handoff.updated_at = now
        await session.flush()
    return SettleResult(SettleOutcome.HELD_UNMEASURED, handoff.state, detail)


async def settle_exit_handoff(
    session: AsyncSession,
    handoff_id: int,
    *,
    expected_claim_count: int,
    reading: BrokerOrderReading | None,
    now: datetime,
) -> SettleResult:
    """Decide what a claimed card proved. The caller commits.

    ``reading`` is the caller's broker read of the card's order, taken before
    this call so that no network wait happens under a lock; ``None`` when it was
    not needed or could not be made.

    The card's proposal row is locked without waiting. If an approval holds it,
    nothing is proven yet and the handoff is left as it is.
    """
    first = await session.get(ExecutionExitHandoff, handoff_id)
    if first is None:
        return SettleResult(SettleOutcome.NOT_CLAIMED, "missing")
    await acquire_sleeve_lock(session, first.sleeve_id)
    handoff = await _locked(session, handoff_id)
    if (
        handoff is None
        or handoff.state != "card_claimed"
        or handoff.proposal_id is None
        or handoff.claim_count != expected_claim_count
    ):
        return SettleResult(
            SettleOutcome.NOT_CLAIMED, "missing" if handoff is None else handoff.state
        )
    group = await session.scalar(
        select(OrderProposal)
        .where(OrderProposal.proposal_id == handoff.proposal_id)
        .with_for_update(skip_locked=True)
    )
    if group is None:
        return SettleResult(SettleOutcome.HELD, handoff.state, "card_row_busy")
    rows = await session.execute(
        select(OrderProposalRung)
        .where(OrderProposalRung.proposal_pk == group.id)
        .order_by(OrderProposalRung.rung_index)
    )
    rungs = list(rows.scalars().all())

    # O1, O3: the card ended and provably never reached the broker.
    if card_proven_unsent(handoff, group, rungs):
        _release(handoff, state="ready_for_card", now=now)
        await session.flush()
        return SettleResult(SettleOutcome.RETURNED_UNSENT, handoff.state)

    rung = rungs[0] if len(rungs) == 1 else None
    order_uuid = (rung.broker_order_id or "").strip() or None if rung else None
    may_have_sent = (
        handoff.send_attempts != handoff.send_attempts_proven_unsent
        or order_uuid is not None
    )
    if not may_have_sent:
        # Still approvable, or ended in a state that proves nothing by itself.
        return SettleResult(SettleOutcome.HELD, handoff.state, "card_not_ended")

    # O2, O4: a card that may have sent needs the broker and the ledger to agree.
    sleeve = await session.get(StrategySleeve, handoff.sleeve_id)
    attributed: Decimal | None = None
    if sleeve is not None and rung is not None:
        try:
            async with session.begin_nested():
                inputs = await load_sleeve_inputs(session, sleeve)
            if rung.id not in inputs.unattributed_rungs:
                attributed = sum(
                    (f.quantity for f in inputs.fills if f.rung_id == rung.id), ZERO
                )
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger proves nothing
            logger.warning(
                "exit_handoff.ledger_unreadable",
                extra={"handoff_id": handoff_id, "exception_type": type(exc).__name__},
            )
    problem = sent_card_problem(
        order_uuid=order_uuid, reading=reading, ledger_attributed=attributed
    )
    if problem is not None or sleeve is None:
        return await _hold_unmeasured(
            session, handoff, now=now, detail=problem or "sleeve_unreadable"
        )

    # Proven. The card's own order no longer makes the sleeve unmeasured.
    handoff.fills_unmeasured = False
    await session.flush()
    holdings = await proven_holdings(session, sleeve, handoff.symbol)
    if holdings is None:
        return await _hold_unmeasured(
            session, handoff, now=now, detail="holdings_unmeasured"
        )
    if holdings > 0:
        _release(handoff, state="ready_for_card", now=now)
        await session.flush()
        return SettleResult(SettleOutcome.RETURNED_REMAINDER, handoff.state)
    _release(handoff, state="closed", now=now)
    await session.flush()
    return SettleResult(SettleOutcome.CLOSED, handoff.state)
