"""Fill models v1: a bracket plus two capacity/empirical variants.

A resting limit order's fate is uncertain from bars alone, so the contract
reports a *bracket* instead of one number:

* ``OptimisticTouch`` -- a print at the limit fills. Uses every bar that
  *overlaps* the order's life (including the bar it was placed in).
* ``ConservativeThrough`` -- price must trade one tick *through* the limit, so
  the queue at the limit is certainly exhausted. Uses only bars *wholly inside*
  the order's life.

Both ends fill a gap at the first price the order could trade at (a buy whose
first reachable price is already at/below the limit fills there).

* ``VolumeShare`` -- the optimistic touch gate, capped at a participation share
  of the volume traded in the same window. Needs window volume; without it the
  model is unavailable rather than guessing.
* ``EmpiricalFillModel`` -- a fit *hook* only. v1 never claims a fitted model:
  below ``MIN_FIT_OBSERVATIONS`` the fit reports ``insufficient_sample``; above
  it the fitting procedure is still unimplemented. ``fill`` always refuses.

Partial fills come only from ``VolumeShare``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Protocol, runtime_checkable

from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    ContractError,
    Side,
    sha256_canonical,
)


class FillModelUnavailable(ContractError):
    """The model cannot produce a fill for this input (reported, not guessed)."""


class FillInputMissing(FillModelUnavailable):
    pass


class EmpiricalModelNotFitted(FillModelUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class FillContext:
    """What the bars say about one order's life.

    ``*_extreme`` is the low (buy) or high (sell) over the window; ``None``
    means the window holds no tradable bar. ``first_price`` is the first price
    the order could trade at (e.g. the open when the order rests from before
    the open); ``None`` when the order was placed mid-bar.
    """

    side: Side
    limit: Decimal
    quantity: Decimal
    tick: Decimal
    optimistic_extreme: Decimal | None
    conservative_extreme: Decimal | None
    first_price: Decimal | None = None
    optimistic_window_volume: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("limit", "quantity", "tick"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ContractError(f"{name} must be a positive finite Decimal")


@dataclass(frozen=True, slots=True)
class FillOutcome:
    filled_qty: Decimal
    price: Decimal | None
    reason: str

    def __post_init__(self) -> None:
        if (self.filled_qty > 0) != (self.price is not None):
            raise ContractError("a fill has a price iff it has a quantity")

    @property
    def any_fill(self) -> bool:
        return self.filled_qty > 0


NO_FILL = "no_fill"


@runtime_checkable
class FillModel(Protocol):
    model_id: str

    def params(self) -> dict[str, Any]: ...

    def fill(self, ctx: FillContext) -> FillOutcome: ...


def params_sha256(model: FillModel) -> str:
    return sha256_canonical({"model_id": model.model_id, "params": model.params()})


def _crosses(side: Side, price: Decimal | None, threshold: Decimal) -> bool:
    if price is None:
        return False
    return price <= threshold if side is Side.BUY else price >= threshold


def _price_fill(
    ctx: FillContext, *, extreme: Decimal | None, threshold: Decimal
) -> FillOutcome:
    """Full fill at the first reachable price (gap) or at the limit."""

    if _crosses(ctx.side, ctx.first_price, threshold):
        assert ctx.first_price is not None
        return FillOutcome(ctx.quantity, ctx.first_price, "gap_at_first_price")
    if _crosses(ctx.side, extreme, threshold):
        return FillOutcome(ctx.quantity, ctx.limit, "limit")
    return FillOutcome(Decimal(0), None, NO_FILL)


@dataclass(frozen=True, slots=True)
class OptimisticTouch:
    model_id: str = BRACKET_OPTIMISTIC

    def params(self) -> dict[str, Any]:
        return {"window": "overlapping", "rule": "touch"}

    def fill(self, ctx: FillContext) -> FillOutcome:
        return _price_fill(ctx, extreme=ctx.optimistic_extreme, threshold=ctx.limit)


@dataclass(frozen=True, slots=True)
class ConservativeThrough:
    model_id: str = BRACKET_CONSERVATIVE

    def params(self) -> dict[str, Any]:
        return {"window": "wholly_inside", "rule": "one_tick_through"}

    def fill(self, ctx: FillContext) -> FillOutcome:
        through = ctx.limit - ctx.tick if ctx.side is Side.BUY else ctx.limit + ctx.tick
        return _price_fill(ctx, extreme=ctx.conservative_extreme, threshold=through)


@dataclass(frozen=True, slots=True)
class VolumeShare:
    participation: Decimal = Decimal("0.1")
    lot: Decimal = Decimal(1)
    model_id: str = "volume_share"

    def __post_init__(self) -> None:
        if not Decimal(0) < self.participation <= Decimal(1):
            raise ContractError("participation must be in (0, 1]")
        if self.lot <= 0:
            raise ContractError("lot must be positive")

    def params(self) -> dict[str, Any]:
        return {
            "gate": "optimistic_touch",
            "participation": self.participation,
            "lot": self.lot,
        }

    def fill(self, ctx: FillContext) -> FillOutcome:
        if ctx.optimistic_window_volume is None:
            raise FillInputMissing("volume_share needs optimistic_window_volume")
        touched = OptimisticTouch().fill(ctx)
        if not touched.any_fill:
            return touched
        capacity = ctx.optimistic_window_volume * self.participation
        lots = (capacity / self.lot).to_integral_value(rounding=ROUND_FLOOR)
        qty = min(ctx.quantity, lots * self.lot)
        if qty <= 0:
            return FillOutcome(Decimal(0), None, "no_capacity")
        reason = touched.reason if qty == ctx.quantity else "partial_capacity"
        return FillOutcome(qty, touched.price, reason)


MIN_FIT_OBSERVATIONS = 30


@dataclass(frozen=True, slots=True)
class FillObservation:
    """One real order: what the bars said and whether the broker filled it."""

    context: FillContext
    filled_qty: Decimal


@dataclass(frozen=True, slots=True)
class EmpiricalFitStatus:
    status: str  # insufficient_sample | fit_not_implemented
    n_observations: int
    n_filled: int
    min_required: int = MIN_FIT_OBSERVATIONS


@dataclass(frozen=True, slots=True)
class EmpiricalFillModel:
    """Hook for a model fitted on real order outcomes. Never fitted in v1."""

    model_id: str = "empirical"

    def params(self) -> dict[str, Any]:
        return {"fitted": False, "min_fit_observations": MIN_FIT_OBSERVATIONS}

    @staticmethod
    def fit(observations: Sequence[FillObservation]) -> EmpiricalFitStatus:
        n = len(observations)
        n_filled = sum(o.filled_qty > 0 for o in observations)
        if n < MIN_FIT_OBSERVATIONS:
            return EmpiricalFitStatus("insufficient_sample", n, n_filled)
        return EmpiricalFitStatus("fit_not_implemented", n, n_filled)

    def fill(self, ctx: FillContext) -> FillOutcome:
        raise EmpiricalModelNotFitted(
            "empirical fill model is a v1 fit hook; no fitted model exists"
        )


BRACKET: tuple[FillModel, FillModel] = (OptimisticTouch(), ConservativeThrough())
DEFAULT_MODELS: tuple[FillModel, ...] = (
    OptimisticTouch(),
    ConservativeThrough(),
    VolumeShare(),
    EmpiricalFillModel(),
)
