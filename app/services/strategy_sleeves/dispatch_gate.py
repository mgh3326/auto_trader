"""The sleeve gate as dispatch runs it (ROB-1470).

``dispatch_proposal`` calls ``evaluate_sleeve_for_dispatch`` for a proposal whose
``sleeve_id`` is set, before revalidation and so before any broker call. An
untagged proposal never reaches this module.

* The per-sleeve advisory lock is taken first, outside any savepoint, and held
  until the dispatch transaction commits -- after revalidation has recorded this
  proposal's broker outcome. A second dispatch of the same sleeve therefore
  measures the first one's order as open, never as absent.
* The measurement runs in a savepoint. Any exception rolls back only that
  savepoint and becomes ``sleeve_unreadable``: the proposal goes to the human
  card, and the dispatch transaction stays usable for the card's audit write.
* ``sleeve_loss_limit_reached`` on a live sleeve appends a ``stopped`` version
  in its own savepoint. If that write fails the verdict still holds.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.strategy_sleeves.book import (
    SleeveOrder,
    compute_sleeve_book,
    order_notional,
)
from app.services.strategy_sleeves.gate import SleeveGateVerdict, evaluate_sleeve_gate
from app.services.strategy_sleeves.marks import MarkFn, latest_close_marks
from app.services.strategy_sleeves.store import (
    acquire_sleeve_lock,
    current_version,
    get_sleeve,
    load_sleeve_inputs,
    sleeve_symbol,
    stop_at_loss_limit,
    version_view,
)
from app.services.strategy_sleeves.vocabulary import SleeveGateReason, SleeveState

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


def _order_buy_notional(group: Any, rungs: list[Any]) -> tuple[bool, Decimal | None]:
    """(places_buy, notional of this proposal's pending buy rungs or None)."""
    if (getattr(group, "action", None) or "place") == "cancel":
        return False, ZERO
    total = ZERO
    places_buy = False
    for rung in rungs:
        if rung.state != "pending_approval" or rung.side != "buy":
            continue
        places_buy = True
        notional = order_notional(
            SleeveOrder(
                proposal_pk=group.id,
                rung_id=rung.id,
                symbol=str(group.symbol),
                side="buy",
                state=rung.state,
                quantity=Decimal(str(rung.quantity)),
                limit_price=(
                    None if rung.limit_price is None else Decimal(str(rung.limit_price))
                ),
                notional=None if rung.notional is None else Decimal(str(rung.notional)),
                rung_filled_qty=None,
            )
        )
        if notional is None:
            return True, None
        total += notional
    return places_buy, total


async def _measure(
    session: AsyncSession,
    *,
    group: Any,
    rungs: list[Any],
    now: Any,
    mark_fn: MarkFn,
) -> tuple[SleeveGateVerdict, Any]:
    sleeve = await get_sleeve(session, group.sleeve_id)
    if (
        sleeve is None
        or sleeve.account_mode != group.account_mode
        or sleeve.market != group.market
    ):
        return SleeveGateVerdict(False, SleeveGateReason.UNREADABLE), None
    row = await current_version(session, sleeve.id)
    if row is None:
        return SleeveGateVerdict(False, SleeveGateReason.UNREADABLE), None
    view = version_view(row)
    places_buy, buy_notional = _order_buy_notional(group, rungs)
    if view.state != SleeveState.LIVE:
        return (
            evaluate_sleeve_gate(
                version=view,
                book=None,
                order_buy_notional=buy_notional,
                places_buy=places_buy,
            ),
            view,
        )
    inputs = await load_sleeve_inputs(session, sleeve)
    unmarked = compute_sleeve_book(orders=inputs.orders, fills=inputs.fills, marks={})
    held = [position.symbol for position in unmarked.positions]
    marks = await mark_fn(session, sleeve.market, held, now) if held else {}
    marks = {sleeve_symbol(k, sleeve.market): v for k, v in marks.items()}
    book = compute_sleeve_book(
        orders=inputs.orders,
        fills=inputs.fills,
        marks=marks,
        exclude_proposal_pk=group.id,
    )
    verdict = evaluate_sleeve_gate(
        version=view,
        book=book,
        order_buy_notional=buy_notional,
        places_buy=places_buy,
    )
    return verdict, view


async def evaluate_sleeve_for_dispatch(
    session: AsyncSession,
    *,
    group: Any,
    rungs: list[Any],
    now: Any,
    mark_fn: MarkFn | None = None,
) -> SleeveGateVerdict:
    sleeve_id = int(group.sleeve_id)
    mark_fn = mark_fn or latest_close_marks
    await acquire_sleeve_lock(session, sleeve_id)
    try:
        async with session.begin_nested():
            verdict, view = await _measure(
                session, group=group, rungs=rungs, now=now, mark_fn=mark_fn
            )
    except Exception as exc:  # noqa: BLE001 - an unreadable sleeve gets a person
        logger.warning(
            "strategy_sleeves.dispatch_gate_unreadable",
            extra={"sleeve_id": sleeve_id, "exception_type": type(exc).__name__},
        )
        return SleeveGateVerdict(
            False, SleeveGateReason.UNREADABLE, {"sleeve_id": str(sleeve_id)}
        )
    if verdict.stop_sleeve and view is not None:
        try:
            async with session.begin_nested():
                await stop_at_loss_limit(
                    session,
                    expected=view,
                    reason=(
                        f"loss reached the loss limit at dispatch of proposal "
                        f"{group.proposal_id}"
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - the verdict already demotes
            logger.error(
                "strategy_sleeves.loss_stop_write_failed",
                extra={"sleeve_id": sleeve_id, "exception_type": type(exc).__name__},
            )
    return verdict
