"""Shared Decimal quantization helpers for monetary and quantity values.

All functions use ROUND_HALF_UP and accept Decimal | float | int inputs.
The str() coercion avoids float binary representation issues.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SCALE_MONEY = Decimal("0.0001")
_SCALE_CRYPTO_QTY = Decimal("0.00000001")
_SCALE_PCT = Decimal("0.01")


def quantize_money(val: Decimal | float | int) -> Decimal:
    """Quantize a monetary amount to 4 decimal places (ROUND_HALF_UP)."""
    return Decimal(str(val)).quantize(_SCALE_MONEY, rounding=ROUND_HALF_UP)


def quantize_crypto_qty(val: Decimal | float | int) -> Decimal:
    """Quantize a cryptocurrency quantity to 8 decimal places (ROUND_HALF_UP)."""
    return Decimal(str(val)).quantize(_SCALE_CRYPTO_QTY, rounding=ROUND_HALF_UP)


def quantize_pct(val: Decimal | float | int) -> Decimal:
    """Quantize a percentage to 2 decimal places (ROUND_HALF_UP)."""
    return Decimal(str(val)).quantize(_SCALE_PCT, rounding=ROUND_HALF_UP)
