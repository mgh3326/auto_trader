"""ROB-321 PR4a — pure exit decision for a long scalping position.

``decide_exit`` is deterministic over the current quote + elapsed hold time.
Long-only: the exit is evaluated on the **bid** (the price we can actually sell
into), never the last trade or the mid — a conservative trigger that avoids the
"mid looked good but the book wasn't there" fill fallacy. Returns the exit
reason or None (hold).
"""

from __future__ import annotations

from decimal import Decimal

# Stable, append-only exit reason codes (persisted to ledger.exit_reason).
TAKE_PROFIT = "take_profit"
STOP_LOSS = "stop_loss"
TIME_STOP = "time_stop"


def decide_exit(
    *,
    bid: Decimal | None,
    last_price: Decimal | None,
    tp_price: Decimal,
    sl_price: Decimal,
    elapsed_seconds: float,
    max_hold_seconds: float,
) -> str | None:
    """Return an exit reason for a long position, or None to keep holding.

    Priority: take-profit, then stop-loss, then time-stop. ``bid`` is the
    conservative sell reference; ``last_price`` is a fallback only when no bid
    is available yet.
    """
    sell_ref = bid if bid is not None else last_price
    if sell_ref is not None:
        if sell_ref >= tp_price:
            return TAKE_PROFIT
        if sell_ref <= sl_price:
            return STOP_LOSS
    if elapsed_seconds >= max_hold_seconds:
        return TIME_STOP
    return None
