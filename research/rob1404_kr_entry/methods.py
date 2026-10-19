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
from datetime import date

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import leg_filled, low_after, tick_floor
from research.rob1383_buy_drought.inputs import TossOrder
from research.rob1383_buy_drought.prices import KST, session_bounds
from research.rob1383_buy_drought.q2_q3_orders import (
    NEAR_LEG_PCT,
    NEAR_LEG_WEIGHT,
    RESUBMIT_SESSIONS,
    order_session_positions,
    reference_price,
)
from research.rob1404_kr_entry.clock import (
    CLOSING_AUCTION_CUTOFF,
    SessionCalendar,
    bar_state,
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


def _finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)


def classify_limit(
    low_strict: float | None, low_incl: float | None, limit: float
) -> tuple[str, str]:
    """Three-state fill. A missing *or non-finite* low is never evidence that
    price stayed above the limit (tester r1 B2: NaN must not become cash)."""

    if _finite(low_strict) and leg_filled("kr", low_strict, limit, "trade_through"):
        return FILLED, "trade_through"
    if not _finite(low_incl):
        return UNKNOWN, "no_bars_in_window"
    if not leg_filled("kr", low_incl, limit, "touch"):
        return UNFILLED, "low_above_limit"
    return UNKNOWN, "touch_without_trade_through"


# ``core.low_after`` bases that measure exactly the requested window.
EXACT_BASES = frozenset({"daily", "hourly"})


def hourly_covers_session(
    hourly: pd.DataFrame | None, bars: pd.DataFrame, pos: int
) -> bool:
    """Does the hourly cache hold *every* bar of session ``pos``?

    ``finalise_hourly`` stretches each bar's ``end_ts`` to the next bar's start,
    so a dropped middle hour is invisible in ``end_ts``; coverage is checked on
    the start times instead: exactly ``open + k h`` for every whole hour of the
    session (KRX: 09:00..14:00, the last bar folded to 15:30), all lows finite.
    """

    if hourly is None:
        return False
    day = hourly[hourly["session_date"] == bars["session_date"].iloc[pos]]
    open_ts = pd.Timestamp(bars["open_ts"].iloc[pos])
    close_ts = pd.Timestamp(bars["close_ts"].iloc[pos])
    n = int((close_ts - open_ts) // pd.Timedelta(hours=1))
    want = [open_ts + pd.Timedelta(hours=k) for k in range(n)]
    have = [pd.Timestamp(x) for x in day["start_ts"]]
    lows = day["low"].to_numpy(float)
    return have == want and bool(len(lows)) and bool(np.isfinite(lows).all())


def lifetime_evidence(
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    life: list[int],
    o: TossOrder,
    cal: SessionCalendar,
) -> tuple[list[tuple[int, float]], float | None]:
    """Per-session strict lows that are exact, and a complete optimistic low.

    * strict (fill evidence): only sessions whose post-order low is measured
      exactly (whole session inside the lifetime -> daily low; else hourly bars
      that start after the order). A partial session without hourly bars
      (``daily_fallback``) may hold a low printed *before* the order, so it is
      no fill evidence (tester r1 B1). Observed hours are real evidence even if
      other hours are missing.
    * optimistic (proof that price never reached the limit): needs *every*
      calendar session the lifetime overlaps to have a stock bar (tester r2
      B1); per session it uses the hourly window low only when the hourly
      cache covers the whole session, else the daily low, which is <= any
      window low and so still a valid bound (tester r2 B2). Any gap or
      non-finite low -> ``None`` (no "never touched" proof).
    """

    end = o.canceled_at or o.filled_at
    start_ts, end_ts = pd.Timestamp(o.ordered_at), pd.Timestamp(end)
    overlap_dates = [
        d
        for d in cal.dates
        if pd.Timestamp(session_bounds("kr", d)[0]) < end_ts
        and pd.Timestamp(session_bounds("kr", d)[1]) > start_ts
    ]
    complete = overlap_dates == [bars["session_date"].iloc[p] for p in life]
    strict: list[tuple[int, float]] = []
    incl: list[float] = []
    for p in life:
        low, basis = low_after(bars, hourly, p, o.ordered_at, end, inclusive=False)
        if basis in EXACT_BASES and _finite(low):
            strict.append((p, float(low)))
        low_i, basis_i = low_after(bars, hourly, p, o.ordered_at, end, inclusive=True)
        if basis_i == "hourly" and not hourly_covers_session(hourly, bars, p):
            low_i, basis_i = float(bars["low"].iloc[p]), "daily_bound"
        if _finite(low_i) and basis_i != "hourly_empty":
            incl.append(float(low_i))
        else:
            complete = False
    return strict, (min(incl) if complete and incl else None)


def _near_leg(
    c: Candidate,
    o: TossOrder,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    shares: float,
    cal: SessionCalendar,
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
    strict, low_incl = lifetime_evidence(bars, hourly, life, o, cal)
    low_strict = min((low for _, low in strict), default=None)
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
    p = next(p for p, low in strict if leg_filled("kr", low, near, "trade_through"))
    at = first_touch_hour(
        hourly, bars["session_date"].iloc[p], near, start=start, end=end
    )
    if at is None:
        # Exact daily evidence (session wholly inside the lifetime) but no
        # hourly bar shows the hour: the session open, which is after the order.
        at, reason = (
            pd.Timestamp(bars["open_ts"].iloc[p]),
            reason + ":fill_hour_unknown",
        )
    if at < start:
        return Leg(near, shares, UNKNOWN, reason="fill_time_before_order"), detail
    return Leg(near, shares, FILLED, at=at, pos=p, reason=reason), detail


def _resubmit_leg(
    c: Candidate,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    cal: SessionCalendar,
) -> tuple[Leg | None, str | None]:
    """Original anchor for the next three *calendar* sessions after expiry."""

    sessions = cal.opening_at_or_after(c.expired_at)[:RESUBMIT_SESSIONS]
    for i in sessions:
        p, state = bar_state(bars, cal.dates[i])
        if p is None:
            if state == "beyond_data_end":
                return None, "immature:resubmit_window_censored"
            return (
                Leg(c.anchor, c.quantity, UNKNOWN, reason="resubmit_bar_missing"),
                None,
            )
        low = float(bars["low"].iloc[p])
        if not _finite(low):
            return (
                Leg(c.anchor, c.quantity, UNKNOWN, reason="nonfinite_low"),
                None,
            )
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
    if len(sessions) < RESUBMIT_SESSIONS:
        return None, "immature:resubmit_window_censored"
    return Leg(c.anchor, c.quantity, UNFILLED, reason="low_above_limit"), None


def _expected_entry_date(name: str, cal: SessionCalendar, signal_at) -> date | None:
    """The calendar session the entry must print on (``None``: no session yet).

    Derived from the signal time alone -- never from where the entry function
    happened to land (tester r2 S2: a missing D0 bar must not be read as a
    roll to the next session)."""

    t = pd.Timestamp(signal_at)
    if name == "next_day_open":
        i = cal.first_after_date(t.tz_convert(KST).date())
        return None if i is None else cal.dates[i]
    i = cal.d0(signal_at)
    if i is None:
        return None
    d0_open = pd.Timestamp(session_bounds("kr", cal.dates[i])[0])
    if t >= d0_open and t.tz_convert(KST).time() >= CLOSING_AUCTION_CUTOFF:
        i += 1  # after the closing-auction cutoff: the next session's open
        if i >= len(cal.dates):
            return None
    return cal.dates[i]


def build_methods(
    c: Candidate,
    o: TossOrder,
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    cal: SessionCalendar,
) -> dict[str, MethodLegs]:
    out: dict[str, MethodLegs] = {"cash": MethodLegs("cash", ())}
    anchor_leg = Leg(c.anchor, c.quantity, UNFILLED, reason="broker_status_unfilled")
    out["anchor_actual"] = MethodLegs("anchor_actual", (anchor_leg,))
    for name, fn in (("nep", next_executable_price), ("next_day_open", next_day_open)):
        e = fn(bars, hourly, c.signal_at)
        want = _expected_entry_date(name, cal, c.signal_at)
        if want is None:
            out[name] = MethodLegs(name, (), missing="immature:no_session_after_signal")
            continue
        if e is not None and not _finite(e.price):
            out[name] = MethodLegs(name, (), missing="data_missing:entry_nonfinite")
            continue
        if e is None or bars["session_date"].iloc[e.pos] != want:
            _, state = bar_state(bars, want)
            out[name] = MethodLegs(
                name,
                (),
                missing=(
                    "immature:entry_beyond_data_end"
                    if state == "beyond_data_end"
                    else "data_missing:entry_bar_missing"
                ),
            )
            continue
        out[name] = MethodLegs(
            name,
            (Leg(e.price, c.quantity, FILLED, True, e.at, e.pos, e.basis),),
            detail={"entry_basis": e.basis},
        )
    k = near_shares(c.quantity)
    if k == 0:
        out["split_int"] = MethodLegs(
            "split_int", (anchor_leg,), detail={"near_shares": 0}
        )
    else:
        leg, detail = _near_leg(c, o, bars, hourly, k, cal)
        rest = Leg(c.anchor, c.quantity - k, UNFILLED, reason="broker_status_unfilled")
        out["split_int"] = (
            MethodLegs("split_int", (), missing="immature:no_session_after_signal")
            if leg is None
            else MethodLegs(
                "split_int", (leg, rest), detail={**detail, "near_shares": k}
            )
        )
    leg, detail = _near_leg(c, o, bars, hourly, 1.0, cal)
    if leg is None:
        out["split_frac"] = MethodLegs(
            "split_frac", (), missing="immature:no_session_after_signal"
        )
    else:
        frac_shares = NEAR_LEG_WEIGHT * c.anchor * c.quantity
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
    rleg, missing = _resubmit_leg(c, bars, hourly, cal)
    out["resubmit3"] = MethodLegs(
        "resubmit3", (rleg,) if rleg is not None else (), missing=missing
    )
    return out
