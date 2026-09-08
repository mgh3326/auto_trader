"""The three options' payoffs — pure arithmetic, no data access.

Accounting convention
---------------------
The position is normalised to 1.0 unit of the asset held at the decision
price ``p0``, with no cash.  Every option is valued at the end of the window
as ``coin * exit_price + cash``.  Dividing by ``p0`` makes the three
comparable across symbols and price scales.

  (1) hold   : 1.0 unit                       -> pt
  (2) trim   : sell f at p0                   -> (1-f)*pt + f*p0
  (3) rebid  : (2) plus buy f back at L if the window low reaches L
               -> pt + f*(p0 - L)   when filled, otherwise identical to (2)
"""

from __future__ import annotations

from dataclasses import dataclass

from .spec import TRIM_FRACTION


@dataclass(frozen=True)
class OptionValues:
    """Window-end portfolio value per unit of the initial position value."""

    hold: float
    trim: float
    rebid: float | None
    rebuy_filled: bool
    rebuy_price: float | None


def option_values(
    *,
    p0: float,
    pt: float,
    rebuy_price: float | None,
    window_low: float | None,
    trim_fraction: float = TRIM_FRACTION,
) -> OptionValues:
    """Value the three options for one observation.

    ``rebuy_price=None`` means no strong support existed below the decision
    price, i.e. option (3) was not available.  That is recorded as ``None``
    rather than silently falling back to option (2) — the pre-registration
    calls for "③ 불가 기록".
    """
    if p0 <= 0:
        raise ValueError("decision price must be positive")
    if not 0 < trim_fraction < 1:
        raise ValueError("trim fraction must be strictly between 0 and 1")

    hold = pt
    trim = (1.0 - trim_fraction) * pt + trim_fraction * p0

    if rebuy_price is None:
        return OptionValues(hold, trim, None, False, None)
    if rebuy_price >= p0:
        raise ValueError(
            "a rebid support level must sit strictly below the decision price"
        )

    filled = window_low is not None and window_low <= rebuy_price
    rebid = pt + trim_fraction * (p0 - rebuy_price) if filled else trim
    return OptionValues(hold, trim, rebid, filled, rebuy_price)


def normalised(values: OptionValues, p0: float) -> dict[str, float | None]:
    """Window return per option, as a fraction of the initial position value."""
    return {
        "hold": values.hold / p0 - 1.0,
        "trim": values.trim / p0 - 1.0,
        "rebid": None if values.rebid is None else values.rebid / p0 - 1.0,
    }


def deltas_vs_hold(values: OptionValues, p0: float) -> dict[str, float | None]:
    """Each option minus option (1), in units of the initial position value.

    🔴 Cost-basis invariance: both differences are functions of ``p0``, ``pt``
    and ``L`` only.  An underwater average cost ``C`` enters every option as
    the same additive constant and cancels here.  The +10/+20/+30% grid can
    therefore not reorder the three options — it only changes how much loss
    the trim books and whether the position is still underwater at the end.
    ``tests/test_simulate.py::test_cost_basis_does_not_change_ranking`` pins
    this, and the report states it instead of implying a sensitivity that
    does not exist.
    """
    return {
        "trim": (values.trim - values.hold) / p0,
        "rebid": None if values.rebid is None else (values.rebid - values.hold) / p0,
    }


def cost_basis_view(
    values: OptionValues,
    *,
    p0: float,
    cost_premium: float,
    trim_fraction: float = TRIM_FRACTION,
) -> dict[str, float | None]:
    """Metrics that *do* depend on the underwater average cost.

    ``cost_premium`` is how far the average cost sits above the event price,
    so ``C = p0 * (1 + cost_premium)``.
    """
    cost = p0 * (1.0 + cost_premium)
    booked_loss = trim_fraction * (p0 - cost) / cost  # negative: a realised loss
    out: dict[str, float | None] = {
        "cost": cost,
        "realised_loss_on_trim_pct_of_cost": booked_loss,
        "hold_pnl_vs_cost": values.hold / cost - 1.0,
        "trim_pnl_vs_cost": values.trim / cost - 1.0,
        "rebid_pnl_vs_cost": None
        if values.rebid is None
        else values.rebid / cost - 1.0,
    }
    out["hold_still_underwater"] = float(values.hold < cost)
    out["trim_still_underwater"] = float(values.trim < cost)
    out["rebid_still_underwater"] = (
        None if values.rebid is None else float(values.rebid < cost)
    )
    return out
