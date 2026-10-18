"""Arm accounting: spend, net value, budget headroom (prereg §3 M1/M2)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from research.rob1391_us_resubmit_shadow.costs import CostModel
from research.rob1391_us_resubmit_shadow.state import ArmBook

ZERO = Decimal("0")


def arm_spent(
    book: ArmBook, model: CostModel, *, exclude_session: str | None = None
) -> Decimal:
    """Fill notional + buy cost, one virtual/real order per session."""

    total = ZERO
    for session in book.sessions():
        if session == exclude_session:
            continue
        notional = book.session_notional(session)
        total += notional + model.buy_cost(notional)
    return total


def arm_net(book: ArmBook, close: Decimal, model: CostModel) -> Decimal:
    """``D+k net asset - B_i``: holdings marked at ``close`` net of the
    estimated liquidation cost, minus everything spent. Cash earns zero."""

    shares = book.shares
    market_value = close * shares
    return market_value - model.sell_cost(market_value, shares) - arm_spent(book, model)


def session_affordable(
    *,
    book: ArmBook,
    session: str,
    price: Decimal,
    budget: Decimal,
    model: CostModel,
    include_cost: bool,
) -> Callable[[int], int]:
    """``filled_so_far -> extra shares`` keeping total spend within ``budget``."""

    before = arm_spent(book, model, exclude_session=session)
    headroom = budget - before

    def affordable(filled_so_far: int) -> int:
        total = model.affordable_shares(
            price=price, remaining_budget=headroom, include_cost=include_cost
        )
        return max(0, total - filled_so_far)

    return affordable
