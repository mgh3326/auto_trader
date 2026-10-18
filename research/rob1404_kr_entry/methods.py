"""Entry methods as share legs with an explicit three-state fill status.

Every leg is ``filled`` / ``unfilled`` / ``unknown`` -- an ``unknown`` leg is
never folded into ``unfilled`` (= cash, 0 return); it is carried to the
aggregation, which reports it separately and as labelled bounds.

Methods (one ``Candidate`` = one original order; quantity ``q`` = the approved
whole-share count):

* ``cash``          -- no order.
* ``anchor_actual`` -- the order as placed. The broker reports it unfilled
  (status REJECTED/CANCELED, 0 filled), so it is ``unfilled`` by evidence.
* ``nep``           -- ``q`` shares at the next executable price (``clock``).
* ``next_day_open`` -- ``q`` shares at the next trading day's open.
* ``split_int``     -- v1 Q2 20/80 with whole shares: ``floor(0.2 q)`` shares
  at ``tick_floor(reference x 0.995)`` during the original lifetime, the rest
  at the anchor (broker: unfilled). ``q < 5`` -> no near leg at all.
* ``split_frac``    -- v1 Q2 as published (20% of the notional at the near
  limit, fractional shares) -- reconciliation only.
* ``resubmit3``     -- v1 Q3: the original anchor re-placed for the next three
  KRX regular sessions after expiry (gap-down fills at the open).

Limit-leg states use the v1 brackets: ``filled`` needs a trade-through
(``low <= limit - 1 tick``) inside the strict window; ``unfilled`` needs the
optimistic window's low to stay above the limit; anything between (a touch
without a trade-through, or no bars) is ``unknown``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from research.rob1383_buy_drought.core import leg_filled, tick_floor
from research.rob1383_buy_drought.inputs import TossOrder
from research.rob1383_buy_drought.q2_q3_orders import (
    NEAR_LEG_PCT,
    NEAR_LEG_WEIGHT,
    RESUBMIT_SESSIONS,
    lifetime_low,
    order_session_positions,
    reference_price,
)
from research.rob1404_kr_entry.clock import (
    d0_index,
    first_touch_hour,
    next_day_open,
    next_executable_price,
)
from research.rob1404_kr_entry.population import Candidate

METHODS = (
    "cash",
    "anchor_actual",
    "nep",
    "next_day_open",
    "split_int",
    "split_frac",
    "resubmit3",
)
FILLED, UNFILLED, UNKNOWN = "filled", "unfilled", "unknown"


@dataclass(frozen=True, slots=True)
class Leg:
    price: float
    shares: float
    state: str
    marketable: bool = False
    at: pd.Timestamp | None = None  # when the fill price printed (UTC)
    pos: int | None = None  # daily position of the fill session
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MethodLegs:
    method: str
    legs: tuple[Leg, ...]
    missing: str | None = None  # whole method not computable (data)
    detail: dict = field(default_factory=dict)


def near_shares(quantity: int) -> int:
    """Whole shares of the 20% near leg (never more than 20% of the order)."""

    return math.floor(NEAR_LEG_WEIGHT * quantity + 1e-9)


def classify_limit(
    low_strict: float | None, low_incl: float | None, limit: float
) -> tuple[str, str]:
    if low_strict is not None and leg_filled("kr", low_strict, limit, "trade_through"):
        return FILLED, "trade_through"
    if low_incl is None:
        return UNKNOWN, "no_bars_in_window"
    if not leg_filled("kr", low_incl, limit, "touch"):
        return UNFILLED, "low_above_limit"
    return UNKNOWN, "touch_without_trade_through"


def _near_leg(
    c: Candidate,
    o: TossOrder,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    shares: float,
) -> tuple[Leg | None, dict]:
    life = order_session_positions(bars, o)
    if not life:
        d0 = d0_index(bars, c.signal_at)
        if d0 is None:
            return None, {}
        ref, basis = reference_price(bars, hourly, d0, o.ordered_at)
        near = tick_floor("kr", ref * (1 - NEAR_LEG_PCT / 100.0))
        reason = "nxt_only_no_venue_prices" if c.nxt_only else "no_regular_bars_cached"
        return (
            Leg(near, shares, UNKNOWN, reason=reason),
            {"near_limit": near, "reference": ref, "reference_basis": basis},
        )
    ref, basis = reference_price(bars, hourly, life[0], o.ordered_at)
    near = tick_floor("kr", ref * (1 - NEAR_LEG_PCT / 100.0))
    low_strict, _ = lifetime_low(bars, hourly, life, o, inclusive=False)
    low_incl, _ = lifetime_low(bars, hourly, life, o, inclusive=True)
    state, reason = classify_limit(low_strict, low_incl, near)
    detail = {
        "near_limit": near,
        "reference": ref,
        "reference_basis": basis,
        "lifetime_low_strict": low_strict,
        "lifetime_low_incl": low_incl,
    }
    if state != FILLED:
        return Leg(near, shares, state, reason=reason), detail
    start = pd.Timestamp(o.ordered_at)
    end = pd.Timestamp(o.canceled_at)
    for p in life:
        at = first_touch_hour(
            hourly, bars["session_date"].iloc[p], near, start=start, end=end
        )
        if at is not None:
            return Leg(near, shares, FILLED, at=at, pos=p, reason=reason), detail
    # Filled by the daily low but no hourly bar shows the hour: session open.
    p = life[0]
    return (
        Leg(
            near,
            shares,
            FILLED,
            at=pd.Timestamp(bars["open_ts"].iloc[p]),
            pos=p,
            reason=reason + ":fill_hour_unknown",
        ),
        detail,
    )


def _resubmit_leg(
    c: Candidate, bars: pd.DataFrame, hourly: pd.DataFrame | None
) -> tuple[Leg | None, str | None]:
    after = pd.Timestamp(c.expired_at)
    nxt = [
        i for i in range(len(bars)) if pd.Timestamp(bars["open_ts"].iloc[i]) >= after
    ]
    nxt = nxt[:RESUBMIT_SESSIONS]
    for p in nxt:
        low = float(bars["low"].iloc[p])
        if not leg_filled("kr", low, c.anchor, "touch"):
            continue
        if not leg_filled("kr", low, c.anchor, "trade_through"):
            return (
                Leg(
                    c.anchor, c.quantity, UNKNOWN, reason="touch_without_trade_through"
                ),
                None,
            )
        opening = float(bars["open"].iloc[p])
        if opening <= c.anchor:
            return (
                Leg(
                    opening,
                    c.quantity,
                    FILLED,
                    at=pd.Timestamp(bars["open_ts"].iloc[p]),
                    pos=p,
                    reason="gap_down_open",
                ),
                None,
            )
        at = first_touch_hour(hourly, bars["session_date"].iloc[p], c.anchor)
        reason = "trade_through"
        if at is None:
            at, reason = pd.Timestamp(bars["open_ts"].iloc[p]), "fill_hour_unknown"
        return Leg(c.anchor, c.quantity, FILLED, at=at, pos=p, reason=reason), None
    if len(nxt) < RESUBMIT_SESSIONS:
        return None, "resubmit_window_censored"
    return Leg(c.anchor, c.quantity, UNFILLED, reason="low_above_limit"), None


def build_methods(
    c: Candidate, o: TossOrder, bars: pd.DataFrame, hourly: pd.DataFrame | None
) -> dict[str, MethodLegs]:
    out: dict[str, MethodLegs] = {"cash": MethodLegs("cash", ())}
    anchor_leg = Leg(c.anchor, c.quantity, UNFILLED, reason="broker_status_unfilled")
    out["anchor_actual"] = MethodLegs("anchor_actual", (anchor_leg,))
    for name, fn in (("nep", next_executable_price), ("next_day_open", next_day_open)):
        e = fn(bars, hourly, c.signal_at)
        out[name] = (
            MethodLegs(name, (), missing="no_bar_after_signal")
            if e is None
            else MethodLegs(
                name,
                (Leg(e.price, c.quantity, FILLED, True, e.at, e.pos, e.basis),),
                detail={"entry_basis": e.basis},
            )
        )
    k = near_shares(c.quantity)
    if k == 0:
        out["split_int"] = MethodLegs(
            "split_int", (anchor_leg,), detail={"near_shares": 0}
        )
    else:
        leg, detail = _near_leg(c, o, bars, hourly, k)
        rest = Leg(c.anchor, c.quantity - k, UNFILLED, reason="broker_status_unfilled")
        out["split_int"] = (
            MethodLegs("split_int", (), missing="no_bar_after_signal")
            if leg is None
            else MethodLegs(
                "split_int", (leg, rest), detail={**detail, "near_shares": k}
            )
        )
    frac_shares = NEAR_LEG_WEIGHT * c.anchor * c.quantity
    leg, detail = _near_leg(c, o, bars, hourly, 1.0)
    if leg is None:
        out["split_frac"] = MethodLegs("split_frac", (), missing="no_bar_after_signal")
        rleg, missing = _resubmit_leg(c, bars, hourly)
        out["resubmit3"] = MethodLegs(
            "resubmit3", (rleg,) if rleg is not None else (), missing=missing
        )
        return out
    leg = Leg(
        leg.price,
        frac_shares / leg.price,
        leg.state,
        leg.marketable,
        leg.at,
        leg.pos,
        leg.reason,
    )
    rest = Leg(
        c.anchor,
        (1 - NEAR_LEG_WEIGHT) * c.quantity,
        UNFILLED,
        reason="broker_status_unfilled",
    )
    out["split_frac"] = MethodLegs("split_frac", (leg, rest), detail=detail)
    rleg, missing = _resubmit_leg(c, bars, hourly)
    out["resubmit3"] = MethodLegs(
        "resubmit3", (rleg,) if rleg is not None else (), missing=missing
    )
    return out
