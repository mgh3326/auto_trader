"""Deterministic H5 holding decisions from broker-proven remaining quantity."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .sizing import floor_to_step

ExitReason = Literal["hard_stop", "bar_close_stop", "time_exit", "tp1", "tp2"]


@dataclass(frozen=True)
class Holding:
    side: Literal["BUY", "SELL"]
    entry_price: Decimal
    entry_qty: Decimal
    broker_remaining_qty: Decimal
    closed_qty: Decimal
    entered_at: dt.datetime
    completed_bars_held: int
    step_size: Decimal


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    qty: Decimal
    reduce_only: bool = True


def choose_exit(
    holding: Holding,
    *,
    quote_price: Decimal,
    intrabar_low: Decimal | None,
    intrabar_high: Decimal | None,
    completed_bar_close: Decimal | None,
    now: dt.datetime,
) -> ExitDecision | None:
    """Priority: hard stop, bar-close stop, time exit, TP2, TP1.

    TP2 outranks TP1: a quote at or beyond +5%/-5% closes the entire
    broker-proven remainder, even when the +3% half is still outstanding.
    An ambiguous historical bar touching stop and TP is stopped first.
    Every result is reduceOnly, limited to fresh broker-proven remainder.
    """
    if holding.broker_remaining_qty <= 0:
        return None
    if holding.side not in {"BUY", "SELL"} or holding.entry_price <= 0:
        raise ValueError("invalid H5 holding")
    if quote_price <= 0 or now.tzinfo is None or holding.entered_at.tzinfo is None:
        raise ValueError("quote and timestamps must be valid")
    long = holding.side == "BUY"
    entry = holding.entry_price
    hard = entry * (Decimal("0.95") if long else Decimal("1.05"))
    bar_stop = entry * (Decimal("0.97") if long else Decimal("1.03"))
    tp1 = entry * (Decimal("1.03") if long else Decimal("0.97"))
    tp2 = entry * (Decimal("1.05") if long else Decimal("0.95"))
    hard_touched = (
        (intrabar_low is not None and intrabar_low <= hard) or quote_price <= hard
        if long
        else (intrabar_high is not None and intrabar_high >= hard)
        or quote_price >= hard
    )
    bar_stopped = completed_bar_close is not None and (
        completed_bar_close <= bar_stop if long else completed_bar_close >= bar_stop
    )
    if hard_touched:
        reason: ExitReason = "hard_stop"
    elif bar_stopped:
        reason = "bar_close_stop"
    elif holding.completed_bars_held >= 6 or now - holding.entered_at >= dt.timedelta(
        hours=24
    ):
        reason = "time_exit"
    elif quote_price >= tp2 if long else quote_price <= tp2:
        reason = "tp2"
    elif (
        quote_price >= tp1 if long else quote_price <= tp1
    ) and holding.closed_qty < floor_to_step(
        holding.entry_qty * Decimal("0.5"), holding.step_size
    ):
        reason = "tp1"
    else:
        return None
    if reason == "tp1":
        half = floor_to_step(holding.entry_qty * Decimal("0.5"), holding.step_size)
        outstanding = half - holding.closed_qty
        if outstanding <= 0:
            return None
        qty = min(outstanding, holding.broker_remaining_qty)
    else:
        qty = holding.broker_remaining_qty
    qty = floor_to_step(qty, holding.step_size)
    if qty <= 0:
        return None
    return ExitDecision(reason=reason, qty=qty)


def entry_kill_reasons(
    *,
    stop_losses_today: int,
    daily_pnl_usdt: Decimal,
    day_start_nav_usdt: Decimal,
    peak_nav_usdt: Decimal,
    current_nav_usdt: Decimal,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if stop_losses_today >= 3:
        reasons.append("three_stops_today")
    if day_start_nav_usdt <= 0 or peak_nav_usdt <= 0 or current_nav_usdt <= 0:
        reasons.append("nav_unavailable")
    else:
        if daily_pnl_usdt <= -(day_start_nav_usdt * Decimal("0.03")):
            reasons.append("daily_loss_entry_stop")
        if current_nav_usdt <= peak_nav_usdt * Decimal("0.85"):
            reasons.append("mdd_lane_stop")
    return tuple(reasons)
