"""Deterministic, conservative per-side fixture cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "CostModel",
    "OrderSide",
    "KR_COST_WIRED",
    "KR_COST_PARAMS_DECLARED",
    "KRCostParamsUnsetError",
    "require_kr_cost_model",
]

OrderSide = Literal["buy", "sell"]

# KR harness imports CostModel through this module (same path crypto/US use).
# Sealed kr-corpus-v1 does not declare fee/slippage parameters. Wiring is
# present; parameters are explicitly unset so callers cannot silently invent
# them via a default constant.
KR_COST_WIRED = True
KR_COST_PARAMS_DECLARED = False


class KRCostParamsUnsetError(RuntimeError):
    """KR cost parameters are not declared; inventing defaults is forbidden."""


def require_kr_cost_model() -> CostModel:
    """Refuse to invent KR fee/slippage; operator must supply CostModel explicitly."""
    raise KRCostParamsUnsetError(
        "KR cost parameters are not declared in the sealed corpus contract. "
        "Construct CostModel(fee_bp=..., slippage_bp_per_side=...) with "
        "operator-approved values. Inventing defaults is forbidden."
    )


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
