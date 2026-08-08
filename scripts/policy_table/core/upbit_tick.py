"""Upbit KRW tick-size table, expressed as a D3-engine ``TickTable`` (reuse).

The ladder mirrors two existing float-based implementations already in this
repo — ``app.services.brokers.upbit.orders.adjust_price_to_upbit_unit`` and
the Decimal floor-only ``app.services.paper_fills.snap_limit_down`` — but we
do not import either: this module hands the same band data to D3's
``research.kr_corpus.d3_engine.tick.TickTable``, so alignment/validation/
sell-minus-one-tick logic is the *actual D3 code*, not a reimplementation.
``TickTable.from_mapping`` has no SHA gate (that only applies to
``load_tick_table``, which loads the frozen KRX table) so it is safe to feed
a synthetic Upbit payload directly.
"""

from __future__ import annotations

from decimal import Decimal

from research.kr_corpus.d3_engine.tick import TickTable

# (lower_inclusive, upper_exclusive | None, tick) — ascending, matching the
# Upbit KRW price-unit ladder documented in adjust_price_to_upbit_unit.
_UPBIT_KRW_BANDS: tuple[tuple[Decimal, Decimal | None, Decimal], ...] = (
    (Decimal("0"), Decimal("0.01"), Decimal("0.00001")),
    (Decimal("0.01"), Decimal("0.1"), Decimal("0.0001")),
    (Decimal("0.1"), Decimal("1"), Decimal("0.001")),
    (Decimal("1"), Decimal("10"), Decimal("0.01")),
    (Decimal("10"), Decimal("100"), Decimal("0.1")),
    (Decimal("100"), Decimal("1000"), Decimal("1")),
    (Decimal("1000"), Decimal("10000"), Decimal("5")),
    (Decimal("10000"), Decimal("100000"), Decimal("10")),
    (Decimal("100000"), Decimal("500000"), Decimal("50")),
    (Decimal("500000"), Decimal("1000000"), Decimal("100")),
    (Decimal("1000000"), Decimal("2000000"), Decimal("500")),
    (Decimal("2000000"), None, Decimal("1000")),
)


def build_upbit_krw_tick_table() -> TickTable:
    """Build a D3 ``TickTable`` for Upbit KRW-quoted markets."""

    bands = [
        {
            "lower_inclusive": str(lower),
            "upper_exclusive": None if upper is None else str(upper),
            "tick": str(tick),
        }
        for lower, upper, tick in _UPBIT_KRW_BANDS
    ]
    return TickTable.from_mapping({"bands": bands})


__all__ = ["build_upbit_krw_tick_table"]
