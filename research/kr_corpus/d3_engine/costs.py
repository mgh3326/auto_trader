"""Single contract path for D3 neutral scalar transaction costs."""

from __future__ import annotations

from decimal import Decimal

from research.kr_corpus.d3_engine.constants import FEE_RATE


def fee_amount(gross: Decimal, fee_rate: Decimal = FEE_RATE) -> Decimal:
    """Return the side-specific scalar fee for a non-negative gross amount."""

    if not gross.is_finite() or gross < 0:
        raise ValueError("gross must be finite and non-negative")
    if not fee_rate.is_finite() or fee_rate < 0:
        raise ValueError("fee_rate must be finite and non-negative")
    return gross * fee_rate


def cash_required(gross: Decimal, fee_rate: Decimal = FEE_RATE) -> Decimal:
    """Return buy-side cash reserved/paid under the scalar convention."""

    return gross + fee_amount(gross, fee_rate)


def proceeds_after_fee(gross: Decimal, fee_rate: Decimal = FEE_RATE) -> Decimal:
    """Return sell-side proceeds under the same scalar convention."""

    return gross - fee_amount(gross, fee_rate)


def round_trip_basis_points(fee_rate: Decimal = FEE_RATE) -> Decimal:
    """Derive the symmetric round-trip scalar in basis points."""

    if not fee_rate.is_finite() or fee_rate < 0:
        raise ValueError("fee_rate must be finite and non-negative")
    return fee_rate * Decimal(2) * Decimal(10000)
