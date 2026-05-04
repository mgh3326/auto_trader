"""Pure shared safety helpers for preopen approval pilot flows."""

from __future__ import annotations

from decimal import Decimal


def is_kr_equity_symbol(symbol: str) -> bool:
    """Return true for canonical six-digit KR equity symbols."""
    return len(symbol) == 6 and symbol.isdigit()


def is_positive_integer_decimal(value: Decimal | None) -> bool:
    """Return true when value is a positive whole-number Decimal."""
    if value is None:
        return False
    return value > 0 and value == value.to_integral_value()


__all__ = ["is_kr_equity_symbol", "is_positive_integer_decimal"]
