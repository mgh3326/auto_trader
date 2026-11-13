"""Order quantity for a Binance Demo submit that originates from a DB-read Decimal."""

from __future__ import annotations

from decimal import Decimal


def venue_order_quantity(qty: Decimal) -> Decimal:
    """The same value with the trailing zeros of the storage scale removed.

    A quantity read back from a ``Numeric(38, 18)`` column keeps that scale, so
    ``Decimal("0.003980000000000000")`` would be signed by ``format(qty, "f")`` as
    an 18-decimal string, which the venue rejects on precision. The text is cut
    only after the decimal point, so the value is unchanged and integers keep
    their text. The Decimal is rebuilt from that exact text, so no context
    precision rounds it. The caller owns the step-grid check.

    Applied only where a DB-read quantity enters a Demo submit. The clients
    themselves keep ``format(qty, "f")``, because sealed evidence such as D2 relies
    on the exact text it was approved with.
    """
    text = format(qty, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text)
