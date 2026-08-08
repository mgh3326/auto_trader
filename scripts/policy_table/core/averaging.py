"""Market-agnostic averaging-down (물타기) arithmetic — ROB-1230 policy table core.

New logic (not D3-engine reuse): the design doc names this formula explicitly
(08-08 oper-coin session, §5) as the thing the table should pre-compute so a
session stops deriving it by hand.

    A = C * (1 - (p/P)*(1+k)) / k

Derivation: solve new_avg = (C + A) / (C/P + A/p) for new_avg = p*(1+k), i.e.
"how much cash A, added at the current price p, pulls the position's average
cost down to within k of p". C is the current cost basis (== quantity * P),
P the current average price, p the current market price.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION, DECIMAL_ROUNDING


def averaging_math(
    *,
    cost_basis: Decimal,
    average_price: Decimal,
    current_price: Decimal,
    k: Decimal,
) -> dict[str, Decimal | bool]:
    """Return the A(k) additional notional plus the target average it buys.

    ``already_satisfied=True`` (additional_notional=0) when the current
    average is already within k of the current price — no addition is
    needed to hit that band (mirrors the BTC +10% row in the 08-08 session
    table, where A(10%) came out negative and was reported as "0 이미 충족").
    """

    if cost_basis <= 0 or average_price <= 0 or current_price <= 0:
        raise ValueError("averaging_math prices/cost_basis must be positive")
    if k <= 0:
        raise ValueError("averaging_math k must be positive")

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        ratio = current_price / average_price
        raw = cost_basis * (Decimal(1) - ratio * (Decimal(1) + k)) / k
        target_average_price = current_price * (Decimal(1) + k)

    already_satisfied = raw <= 0
    additional_notional = raw if raw > 0 else Decimal(0)
    return {
        "k": k,
        "additional_notional": additional_notional,
        "target_average_price": target_average_price,
        "already_satisfied": already_satisfied,
    }


__all__ = ["averaging_math"]
