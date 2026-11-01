"""Markouts R_h and the cash comparison, shared by every adapter."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from research.eval_contract.contract import HORIZONS, CostModel, Outcome


def cash_return(rate_per_session: Decimal, sessions: int) -> Decimal:
    """Compounded cash return over ``sessions`` sessions."""

    if sessions < 0:
        raise ValueError("sessions must be non-negative")
    if not rate_per_session.is_finite() or rate_per_session <= Decimal(-1):
        raise ValueError("rate_per_session must be finite and > -1")
    return (Decimal(1) + rate_per_session) ** sessions - Decimal(1)


def buy_markouts(
    *,
    order_id: str,
    entry_price: Decimal,
    entry_index: int,
    closes: Sequence[Decimal | None],
    costs: CostModel,
    cash_rate_per_session: Decimal = Decimal(0),
    horizons: Sequence[int] = HORIZONS,
) -> tuple[Outcome, ...]:
    """R_h of a buy filled at ``entry_price`` in session ``entry_index`` (day 0).

    ``closes`` is indexed by session; ``None`` marks a session with no close
    for the symbol (halt, missing bar). ``closes[entry_index + h]`` is the exit;
    a horizon past the end or on a ``None`` is censored, never 0.
    """

    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if not 0 <= entry_index < len(closes) or closes[entry_index] is None:
        raise ValueError("entry_index must point at a session with a close")
    out: list[Outcome] = []
    for h in horizons:
        pos = entry_index + h
        close = closes[pos] if pos < len(closes) else None
        if close is None:
            out.append(Outcome(order_id=order_id, horizon=h, censored=True))
            continue
        gross = close / entry_price - Decimal(1)
        net = costs.sell_proceeds(close) / costs.buy_cash(entry_price) - Decimal(1)
        cash = cash_return(cash_rate_per_session, h)
        out.append(
            Outcome(
                order_id=order_id,
                horizon=h,
                censored=False,
                gross_return=gross,
                net_return=net,
                cash_return=cash,
                net_vs_cash=net - cash,
            )
        )
    return tuple(out)
