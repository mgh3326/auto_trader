"""Pure fill-decision helpers for the paper resting-limit sim (ROB-703).

No I/O, no LLM, no DB — ``Decimal`` in, ``Decimal``/``None`` out. The Upbit KRW
tick bands mirror ``app.services.brokers.upbit.orders.adjust_price_to_upbit_unit``,
but we FLOOR (conservative snap-down) instead of round-to-nearest so the actual
placed price never claims a tick the exchange would not honor.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal

# (threshold, tick unit) — first band whose threshold <= price applies.
_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("500")),
    (Decimal("500000"), Decimal("100")),
    (Decimal("100000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("1000"), Decimal("5")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
)
_MIN_TICK = Decimal("0.00001")


def snap_limit_down(price: Decimal) -> Decimal:
    """Floor ``price`` to the matching Upbit KRW tick band."""
    unit = next((u for thr, u in _TICK_BANDS if price >= thr), _MIN_TICK)
    return (price / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def limit_crossed(
    side: str,
    limit_price: Decimal,
    bars: Sequence[tuple[Decimal, Decimal]],
) -> Decimal | None:
    """Return ``limit_price`` if any bar's range reaches the resting limit.

    ``bars`` is a sequence of ``(low, high)`` tuples ordered oldest -> newest.
    A buy limit fills when any bar's ``low <= limit_price`` (the market dipped
    to or through the bid). A sell limit fills when any bar's ``high >= limit_price``
    (the market rose to or through the ask). Returns ``None`` when no bar
    crosses.
    """
    side = side.lower()
    for low, high in bars:
        if side == "buy" and low <= limit_price:
            return limit_price
        if side == "sell" and high >= limit_price:
            return limit_price
    return None


__all__ = ["snap_limit_down", "limit_crossed"]
