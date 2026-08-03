"""Deterministic, conservative per-side fixture cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["CostModel", "OrderSide"]

OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class CostModel:
    """Fee plus slippage expressed in basis points per side.

    Integer minor units use ceiling division on each side. That makes an
    indivisible unit conservatively more expensive rather than silently losing
    a fractional cost.
    """

    fee_bp: int
    slippage_bp_per_side: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("fee_bp", self.fee_bp),
            ("slippage_bp_per_side", self.slippage_bp_per_side),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative int")

    @property
    def side_bp(self) -> int:
        return self.fee_bp + self.slippage_bp_per_side

    @property
    def round_trip_bp(self) -> int:
        return self.side_bp * 2

    def side_cost_minor_units(self, notional_minor: int, *, side: OrderSide) -> int:
        """Return the rounded-up cost for one buy or sell leg."""
        if side not in ("buy", "sell"):
            raise ValueError(f"unsupported side {side!r}")
        if type(notional_minor) is not int or notional_minor < 0:
            raise ValueError("notional_minor must be a non-negative int")
        return (notional_minor * self.side_bp + 9_999) // 10_000

    def round_trip_cost_minor_units(self, notional_minor: int) -> int:
        """Charge independently rounded buy and sell costs."""
        return self.side_cost_minor_units(
            notional_minor, side="buy"
        ) + self.side_cost_minor_units(notional_minor, side="sell")
