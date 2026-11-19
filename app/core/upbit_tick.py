"""Upbit KRW-market order price unit (호가 단위) as one pure function.

Source: Upbit Developer Center, "원화 마켓 주문 가격 단위"
https://docs.upbit.com/kr/docs/krw-market-info (page updated 2026-05-04, read
2026-11-20). The table is the one in force since the 2025-07-31 change
(https://docs.upbit.com/kr/changelog/krw_tick_unit_change_250731). Each range
is "lower bound inclusive, upper bound exclusive".

The page still links two older per-market notes (USDT/USDC at 0.5 KRW between
1,000 and 10,000 KRW, 2025-03-21; sixteen markets at 1 KRW between 100 and
1,000 KRW, 2024-10-14) and does not say whether they survived the 2025-07-31
change. This function is the general table only; BTC, ETH, SOL and XRP are in
neither note.

``app.services.brokers.upbit.orders.adjust_price_to_upbit_unit`` is an older
table (5 KRW between 1,000 and 5,000 KRW, 500 KRW between 1,000,000 and
2,000,000 KRW) and rounds to the nearest unit. It is left as it is; callers
that already hold a price validated here must not pass it through that
function.

The Go executor keeps the same table; both are checked against
``tests/fixtures/upbit_krw_tick/boundaries.json``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

#: ``(lower bound inclusive, tick)`` from the highest range down.
UPBIT_KRW_TICK_TABLE: Final[tuple[tuple[Decimal, Decimal], ...]] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("1000")),
    (Decimal("500000"), Decimal("500")),
    (Decimal("100000"), Decimal("100")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("1000"), Decimal("1")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
    (Decimal("0.001"), Decimal("0.00001")),
    (Decimal("0.0001"), Decimal("0.000001")),
    (Decimal("0.00001"), Decimal("0.0000001")),
)
_BELOW_TABLE_TICK: Final = Decimal("0.00000001")


def upbit_krw_tick(price: Decimal) -> Decimal:
    """The order price unit for ``price`` on the Upbit KRW market.

    ``price`` must be an exact, finite, positive :class:`~decimal.Decimal`. A
    float is refused: the caller would already have lost the exact price.
    """
    if not isinstance(price, Decimal):
        raise TypeError("price must be a Decimal")
    if not price.is_finite() or price <= 0:
        raise ValueError("price must be a finite positive Decimal")
    for lower_bound, tick in UPBIT_KRW_TICK_TABLE:
        if price >= lower_bound:
            return tick
    return _BELOW_TABLE_TICK


def is_on_upbit_krw_tick(price: Decimal) -> bool:
    """Whether ``price`` is an exact multiple of its own range's unit."""
    return price % upbit_krw_tick(price) == 0
