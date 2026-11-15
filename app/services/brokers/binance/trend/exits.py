"""Deterministic exit decision for a held trend position; pure.

Priority: hard stop, signal flip, time exit. Every exit is a full close of the
broker-proven remainder (reduceOnly is applied by the executor), floored to the
lot step. There is no take-profit and no partial exit by design.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .constants import (
    EXIT_HARD_STOP,
    EXIT_SIGNAL_FLIP,
    EXIT_TIME_EXIT,
    TIME_EXIT_DAYS,
)
from .sizing import floor_to_step

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class HeldPosition:
    side: Side
    entry_price: Decimal
    entered_at: dt.datetime
    #: Broker-proven quantity still open (absolute), from a fresh account read.
    broker_remaining_qty: Decimal
    step_size: Decimal


@dataclass(frozen=True)
class HardStopObservation:
    """What the tick could observe about price since the fill.

    ``minutes_ok`` is False when the 1m fetch failed: the tick then judges the
    hard stop on the completed 4h decision only (``bar_close``), never on a
    partial 1m picture and never on the quote.
    """

    minutes_ok: bool
    minute_low: Decimal | None
    minute_high: Decimal | None
    bar_close: Decimal | None
    quote: Decimal | None


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    qty: Decimal


def hard_stop_touched(
    *, side: Side, stop_price: Decimal, observed: HardStopObservation
) -> bool:
    long = side == "BUY"

    def beyond(price: Decimal | None) -> bool:
        if price is None:
            return False
        return price <= stop_price if long else price >= stop_price

    if beyond(observed.bar_close):
        return True
    if not observed.minutes_ok:
        return False
    extreme = observed.minute_low if long else observed.minute_high
    return beyond(extreme) or beyond(observed.quote)


def choose_exit(
    held: HeldPosition,
    *,
    stop_price: Decimal,
    observed: HardStopObservation,
    signal_side: Side | None,
    now: dt.datetime,
) -> ExitDecision | None:
    if held.broker_remaining_qty <= 0:
        return None
    if held.entry_price <= 0 or stop_price <= 0:
        raise ValueError("invalid held position")
    if now.tzinfo is None or held.entered_at.tzinfo is None:
        raise ValueError("timestamps must be aware")
    if hard_stop_touched(side=held.side, stop_price=stop_price, observed=observed):
        reason = EXIT_HARD_STOP
    elif signal_side is not None and signal_side != held.side:
        reason = EXIT_SIGNAL_FLIP
    elif now - held.entered_at >= dt.timedelta(days=TIME_EXIT_DAYS):
        reason = EXIT_TIME_EXIT
    else:
        return None
    qty = floor_to_step(held.broker_remaining_qty, held.step_size)
    return ExitDecision(reason=reason, qty=qty) if qty > 0 else None
