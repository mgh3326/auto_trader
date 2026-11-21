"""Exit rules, take-profit ladder and re-entry ban (design §4.3, §5.4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal

from app.core.upbit_tick import upbit_krw_tick

from .constants import (
    HARD_STOP_FRACTION,
    MIN_ORDER_KRW,
    REENTRY_BAN_DAYS,
    REENTRY_BAN_LOSS_FRACTION,
    TAKE_PROFIT_MAX_RENEWALS,
    TAKE_PROFIT_RENEWAL_HOURS,
    TAKE_PROFIT_RUNGS,
    TIME_STOP_DECISION_SLOTS,
)
from .sizing import floor_quantity

_PRECISION = 50
EXIT_HARD_STOP = "hard_stop"
EXIT_TREND = "trend_exit"
EXIT_TIME = "time_stop"
ExitReason = Literal["hard_stop", "trend_exit", "time_stop"]


def ceil_to_tick(price: Decimal) -> Decimal:
    """Smallest on-tick price >= ``price`` (a range change re-checks the tick)."""
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ValueError("price must be a finite positive Decimal")
    current = price
    for _ in range(4):
        tick = upbit_krw_tick(current)
        rounded = (current / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        if rounded == current:
            return rounded
        current = rounded
    raise ArithmeticError("ceil_to_tick did not converge")  # unreachable in practice


def hard_stop_price(avg_entry_price: Decimal) -> Decimal:
    """Average entry fill minus 10%; exact, not tick-rounded (it is a trigger)."""
    if not avg_entry_price.is_finite() or avg_entry_price <= 0:
        raise ValueError("avg_entry_price must be a finite positive Decimal")
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return avg_entry_price * (1 - HARD_STOP_FRACTION)


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: ExitReason | None
    #: the best bid was unknown at a check slot, so the stop could not be judged
    hard_stop_unverified: bool
    #: R60 was unknown at a decision slot, so the trend exit could not be judged
    trend_unverified: bool


def evaluate_exit(
    *,
    avg_entry_price: Decimal,
    best_bid_price: Decimal | None,
    is_decision_slot: bool,
    r60: Decimal | None,
    decision_slots_since_fill: int,
) -> ExitDecision:
    """Priority 1 hard stop, 2 trend exit, 3 time stop.

    The stop is judged at every check slot from the best bid; the trend exit
    and time stop only at the 09:10 decision slot. An unknown input never
    triggers an exit and is reported as unverified.
    """
    stop_unverified = best_bid_price is None
    if best_bid_price is not None and best_bid_price <= hard_stop_price(
        avg_entry_price
    ):
        return ExitDecision(True, EXIT_HARD_STOP, False, False)
    trend_unverified = False
    if is_decision_slot:
        if r60 is None:
            trend_unverified = True
        elif r60 <= 0:
            return ExitDecision(True, EXIT_TREND, stop_unverified, False)
        if decision_slots_since_fill >= TIME_STOP_DECISION_SLOTS:
            return ExitDecision(True, EXIT_TIME, stop_unverified, trend_unverified)
    return ExitDecision(False, None, stop_unverified, trend_unverified)


@dataclass(frozen=True)
class LadderRung:
    index: int
    take_profit_return: Decimal
    price_krw: Decimal
    quantity: Decimal
    notional_krw: Decimal
    #: Design is silent on a ladder child below the 5,000 KRW minimum order;
    #: it is flagged here, not dropped, so the caller decides.
    below_min_order: bool


def take_profit_ladder(
    avg_entry_price: Decimal, filled_quantity: Decimal
) -> tuple[LadderRung, ...]:
    """Three rungs at +2/+4/+6%: ``ceil_tick(c * (1 + r))``, 1/3 of the quantity each.

    Quantities floor to 8 decimals; the last rung takes the remainder so the
    rungs add up to exactly ``filled_quantity``.
    """
    if not avg_entry_price.is_finite() or avg_entry_price <= 0:
        raise ValueError("avg_entry_price must be a finite positive Decimal")
    if not filled_quantity.is_finite() or filled_quantity <= 0:
        raise ValueError("filled_quantity must be a finite positive Decimal")
    rung_count = len(TAKE_PROFIT_RUNGS)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        share = floor_quantity(filled_quantity / rung_count)
        rungs: list[LadderRung] = []
        allocated = Decimal(0)
        for index, ret in enumerate(TAKE_PROFIT_RUNGS):
            quantity = filled_quantity - allocated if index == rung_count - 1 else share
            allocated += quantity
            price = ceil_to_tick(avg_entry_price * (1 + ret))
            notional = quantity * price
            rungs.append(
                LadderRung(
                    index, ret, price, quantity, notional, notional < MIN_ORDER_KRW
                )
            )
    return tuple(rungs)


@dataclass(frozen=True)
class LadderCycle:
    #: 0-based renewal number, 0..9
    cycle: int
    opens_at: datetime
    expires_at: datetime


def ladder_cycle(*, ladder_opened_at: datetime, now: datetime) -> LadderCycle | None:
    """The 12-hour intent cycle ``now`` falls in, or ``None`` once 5 days are over.

    The ladder (not each rung) lives at most 10 renewals x 12 hours from the
    moment the entry completed; afterwards the remainder is left to the stop,
    trend exit and time stop. Both datetimes must be timezone-aware.
    """
    if ladder_opened_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    if now < ladder_opened_at:
        raise ValueError("now is before the ladder opened")
    period = timedelta(hours=TAKE_PROFIT_RENEWAL_HOURS)
    cycle = int((now - ladder_opened_at) // period)
    if cycle >= TAKE_PROFIT_MAX_RENEWALS:
        return None
    opens = ladder_opened_at + cycle * period
    return LadderCycle(cycle, opens, opens + period)


def next_open_rung(filled_rung_indexes: frozenset[int]) -> int | None:
    """Rungs are sequential (one open sell intent per market): lowest unfilled."""
    for index in range(len(TAKE_PROFIT_RUNGS)):
        if index not in filled_rung_indexes:
            return index
    return None


def triggers_reentry_ban(
    *,
    reason: ExitReason,
    avg_entry_price: Decimal,
    exit_avg_price: Decimal,
) -> bool:
    """A hard stop, or any close at -4.5% or worse versus the average entry."""
    if reason == EXIT_HARD_STOP:
        return True
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return exit_avg_price / avg_entry_price - 1 <= -REENTRY_BAN_LOSS_FRACTION


def reentry_banned_until(exited_at: datetime) -> datetime:
    if exited_at.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return exited_at + timedelta(days=REENTRY_BAN_DAYS)
