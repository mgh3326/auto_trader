"""Pure sleeve gate verdict (ROB-1470).

A sleeve-tagged proposal reaches this verdict before revalidation, so before any
broker call. Any verdict other than ``allowed`` sends the proposal to the human
approval card with the named reason; it never rejects the order outright and it
never lets an untagged proposal in here.

Order, first match wins:

1. no readable current version          -> ``sleeve_unreadable``
2. state is not ``live``                 -> ``sleeve_not_live``
3. no readable book                      -> ``sleeve_unreadable``
4. P&L unknown (a held symbol unmarked)  -> ``sleeve_pnl_unmeasured``
5. loss >= loss limit                    -> ``sleeve_loss_limit_reached`` (stop)
6. buy: used or order notional unknown   -> ``sleeve_used_unmeasured``
7. buy: used + order notional > budget   -> ``sleeve_budget_exceeded``

Steps 1-5 apply to every tagged order, sells and cancels included: a stopped or
unmeasurable sleeve gets a person for everything. Steps 6-7 apply only to an
order that places a buy, because a sell or a cancel spends no budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.strategy_sleeves.book import SleeveBook
from app.services.strategy_sleeves.vocabulary import SleeveGateReason, SleeveState


@dataclass(frozen=True)
class SleeveVersionView:
    sleeve_id: int
    version: int
    state: str
    budget_cap: Decimal
    loss_limit: Decimal


@dataclass(frozen=True)
class SleeveGateVerdict:
    allowed: bool
    reason: SleeveGateReason | None
    details: dict[str, Any] = field(default_factory=dict)
    #: True only for ``sleeve_loss_limit_reached`` on a live sleeve: the caller
    #: appends a ``stopped`` version.
    stop_sleeve: bool = False


def _text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def evaluate_sleeve_gate(
    *,
    version: SleeveVersionView | None,
    book: SleeveBook | None,
    order_buy_notional: Decimal | None,
    places_buy: bool,
) -> SleeveGateVerdict:
    if version is None:
        return SleeveGateVerdict(False, SleeveGateReason.UNREADABLE)
    details: dict[str, Any] = {
        "sleeve_id": str(version.sleeve_id),
        "sleeve_version": str(version.version),
        "sleeve_state": version.state,
    }
    if version.state != SleeveState.LIVE:
        return SleeveGateVerdict(False, SleeveGateReason.NOT_LIVE, details)
    if book is None:
        return SleeveGateVerdict(False, SleeveGateReason.UNREADABLE, details)
    loss = book.loss
    if loss is None:
        details["sleeve_missing_mark_count"] = str(len(book.missing_marks))
        return SleeveGateVerdict(False, SleeveGateReason.PNL_UNMEASURED, details)
    details["sleeve_net_pnl"] = _text(book.net_pnl or Decimal("0"))
    details["sleeve_loss_limit"] = _text(version.loss_limit)
    if loss >= version.loss_limit:
        return SleeveGateVerdict(
            False, SleeveGateReason.LOSS_LIMIT_REACHED, details, stop_sleeve=True
        )
    if not places_buy:
        return SleeveGateVerdict(True, None, details)
    if book.used is None or order_buy_notional is None:
        return SleeveGateVerdict(False, SleeveGateReason.USED_UNMEASURED, details)
    used_after = book.used + order_buy_notional
    details.update(
        sleeve_used=_text(book.used),
        sleeve_order_notional=_text(order_buy_notional),
        sleeve_used_after=_text(used_after),
        sleeve_budget_cap=_text(version.budget_cap),
    )
    if used_after > version.budget_cap:
        return SleeveGateVerdict(False, SleeveGateReason.BUDGET_EXCEEDED, details)
    return SleeveGateVerdict(True, None, details)
