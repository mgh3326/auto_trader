"""One cycle-metric extractor, applied identically to both calibration sides.

GAP-04's principle — "a different formula makes the comparison itself
meaningless" — is enforced structurally here: the operator's real fills and the
B0 replay's fills are reduced to the same :class:`CycleFill` record and run
through the same reconstruction, the same GAP-03 censoring, the same GAP-07
session assignment, and the same medians. The only thing that differs between
the sides is the fill tape.

Frozen literals this module implements (no invention):

* GAP-01 — ``annualized_cycle_count = closed * 365.2425 / window_calendar_days``.
* GAP-02 — selector lives in the caller; this module is source-agnostic.
* GAP-03 — left-censored (carry-in) and right-censored cycles leave the
  closed-cycle series and are counted, never silently dropped.
* GAP-05 — gross-to-gross; no fee is imputed on either side.
* GAP-06 — an empty sample stays ``NOT_COMPUTABLE``; it never becomes 0.
* GAP-07 — a fill dated outside the sealed XKRX session list is
  ``session_unassignable`` and excluded with its count published.

All arithmetic runs inside :func:`contract_context`, the contract's pinned
50-digit ``ROUND_HALF_UP`` Decimal context — the same one the engine wraps its
own run in. Python's 28-digit default would silently truncate the actual-side
values that the D3-C2P fix-r1 job published and an independent verifier passed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from research.kr_corpus.d3_engine.constants import (
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
)


@contextmanager
def contract_context() -> Iterator[None]:
    """The contract's fixed Decimal arithmetic context (prec 50, HALF_UP)."""

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        yield


def decimal_text(value: Decimal) -> str:
    """Fixed-notation Decimal text; an exponent-only zero renders as ``0``."""

    return format(value, "f")


WINDOW_START = date(2025, 1, 1)
WINDOW_END = date(2025, 12, 31)
WINDOW_CALENDAR_DAYS = (WINDOW_END - WINDOW_START).days + 1
ANNUALIZATION_CALENDAR_DAYS = Decimal("365.2425")

POSITIVE_SCALE = (
    "annualized_cycle_count",
    "holding_period_sessions",
    "add_sizing_multiple",
    "adds_per_cycle",
    "add_interval_sessions",
    "open_lot_age_sessions",
)
BOUNDED_SHARE = ("trim_share", "capital_share")
SIGNED_PERCENTAGE_POINTS = ("signed_realized_pnl_pct",)
METRIC_IDS = (*POSITIVE_SCALE, *BOUNDED_SHARE, *SIGNED_PERCENTAGE_POINTS)

COMPARISON_KIND = {
    **dict.fromkeys(POSITIVE_SCALE, "positive_scale"),
    **dict.fromkeys(BOUNDED_SHARE, "bounded_share"),
    **dict.fromkeys(SIGNED_PERCENTAGE_POINTS, "signed_percentage_points"),
}


@dataclass(frozen=True, slots=True)
class CycleFill:
    """One filled buy or sell, gross-priced, on the shared record shape."""

    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    day: date
    sequence: int

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.price


@dataclass(slots=True)
class Cycle:
    symbol: str
    first_buy_day: date
    first_buy_amount: Decimal
    buy_count: int = 1
    sell_count: int = 0
    closed: bool = False
    close_day: date | None = None

    @property
    def key(self) -> tuple[str, date, int]:
        return (self.symbol, self.first_buy_day, self.buy_count_key)

    buy_count_key: int = 0

    def open_at(self, as_of: date) -> bool:
        if self.first_buy_day > as_of:
            return False
        return not self.closed or (
            self.close_day is not None and self.close_day > as_of
        )


@dataclass(slots=True)
class AddEvent:
    cycle_key: tuple[str, date, int]
    day: date
    previous_buy_day: date
    multiple: Decimal


@dataclass(slots=True)
class SellEvent:
    cycle_key: tuple[str, date, int]
    day: date
    trim_share: Decimal
    pnl_pct: Decimal


@dataclass(slots=True)
class Reconstruction:
    cycles: list[Cycle] = field(default_factory=list)
    adds: list[AddEvent] = field(default_factory=list)
    sells: list[SellEvent] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def reconstruct_cycles(fills: list[CycleFill]) -> Reconstruction:
    """flat -> buy -> ... -> flat cycles, positions merged per symbol.

    An oversell (recorded sell larger than the reconstructed position, which
    the operator archive contains where the true position predates its
    coverage) is clamped to the position and disclosed, never dropped.
    """

    by_symbol: dict[str, list[CycleFill]] = {}
    for fill in sorted(fills, key=lambda item: (item.day, item.sequence, item.symbol)):
        by_symbol.setdefault(fill.symbol, []).append(fill)

    out = Reconstruction()
    with contract_context():
        _walk_symbols(by_symbol, out)
    return out


def _walk_symbols(by_symbol: dict[str, list[CycleFill]], out: Reconstruction) -> None:
    for symbol, symbol_fills in sorted(by_symbol.items()):
        position = Decimal(0)
        average = Decimal(0)
        cycle: Cycle | None = None
        cycle_ordinal = 0
        previous_buy_day: date | None = None
        for fill in symbol_fills:
            if fill.side == "BUY":
                if position == 0:
                    cycle_ordinal += 1
                    cycle = Cycle(
                        symbol=symbol,
                        first_buy_day=fill.day,
                        first_buy_amount=fill.amount,
                        buy_count_key=cycle_ordinal,
                    )
                    out.cycles.append(cycle)
                    average = fill.price
                    position = fill.quantity
                else:
                    assert cycle is not None
                    assert previous_buy_day is not None
                    out.adds.append(
                        AddEvent(
                            cycle_key=cycle.key,
                            day=fill.day,
                            previous_buy_day=previous_buy_day,
                            multiple=fill.amount / cycle.first_buy_amount,
                        )
                    )
                    cycle.buy_count += 1
                    average = (position * average + fill.amount) / (
                        position + fill.quantity
                    )
                    position += fill.quantity
                previous_buy_day = fill.day
                continue
            if position <= 0:
                out.anomalies.append(
                    f"SELL while flat: {symbol} {fill.day} qty={fill.quantity}"
                )
                continue
            sell_quantity = fill.quantity
            if sell_quantity > position:
                out.anomalies.append(
                    f"SELL qty>pos (clamped to position): {symbol} {fill.day} "
                    f"qty={fill.quantity} pos={position}"
                )
                sell_quantity = position
            assert cycle is not None
            out.sells.append(
                SellEvent(
                    cycle_key=cycle.key,
                    day=fill.day,
                    trim_share=sell_quantity / position,
                    pnl_pct=(fill.price - average) / average * Decimal(100),
                )
            )
            cycle.sell_count += 1
            position -= sell_quantity
            if position == 0:
                cycle.closed = True
                cycle.close_day = fill.day
                previous_buy_day = None


@dataclass(slots=True)
class Gap03Classification:
    carry_in_closed: list[Cycle]
    carry_in_open_with_activity: list[Cycle]
    right_censored: list[Cycle]
    eligible_closed: list[Cycle]

    @property
    def carry_in_all(self) -> list[Cycle]:
        return self.carry_in_closed + self.carry_in_open_with_activity

    @property
    def overlap(self) -> list[Cycle]:
        censored = {cycle.key for cycle in self.right_censored}
        return [
            cycle for cycle in self.carry_in_open_with_activity if cycle.key in censored
        ]


def _in_window(value: date | None) -> bool:
    return value is not None and WINDOW_START <= value <= WINDOW_END


def classify_gap03(recon: Reconstruction) -> Gap03Classification:
    closed_in_window = [
        cycle for cycle in recon.cycles if cycle.closed and _in_window(cycle.close_day)
    ]
    right_censored = [cycle for cycle in recon.cycles if cycle.open_at(WINDOW_END)]
    carry_in_closed = [
        cycle for cycle in closed_in_window if cycle.first_buy_day < WINDOW_START
    ]
    activity: set[tuple[str, date, int]] = set()
    for event in recon.adds:
        if _in_window(event.day):
            activity.add(event.cycle_key)
    for event in recon.sells:
        if _in_window(event.day):
            activity.add(event.cycle_key)
    carry_in_open_with_activity = [
        cycle
        for cycle in right_censored
        if cycle.first_buy_day < WINDOW_START and cycle.key in activity
    ]
    eligible_closed = [
        cycle for cycle in closed_in_window if cycle.first_buy_day >= WINDOW_START
    ]
    return Gap03Classification(
        carry_in_closed=carry_in_closed,
        carry_in_open_with_activity=carry_in_open_with_activity,
        right_censored=right_censored,
        eligible_closed=eligible_closed,
    )


def median(values: list[Decimal]) -> tuple[Decimal | None, int]:
    """Exact Decimal median; no rounding before comparison."""

    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None, 0
    if count % 2:
        return ordered[count // 2], count
    with contract_context():
        return (ordered[count // 2 - 1] + ordered[count // 2]) / Decimal(2), count


class SessionAxis:
    """GAP-07 session assignment against the sealed 2025 XKRX calendar."""

    def __init__(self, sessions: tuple[date, ...]) -> None:
        self._index = {session: position for position, session in enumerate(sessions)}
        self.sessions = sessions
        self.cutoff_index = len(sessions) - 1

    def seq(self, day: date | None) -> int | None:
        if day is None:
            return None
        return self._index.get(day)


def _observation(
    values: list[Decimal],
    *,
    raw: int,
    excluded: int,
    note: str | None = None,
) -> dict[str, Any]:
    value, count = median(values)
    payload: dict[str, Any] = {
        "aggregate_kind": "median",
        "aggregate_decimal": decimal_text(value) if value is not None else None,
        "median_decimal": decimal_text(value) if value is not None else None,
        "n": count,
        "raw_observation_count": raw,
        "excluded_observation_count": excluded,
    }
    if note:
        payload["note"] = note
    return payload


def compute_cycle_metrics(
    recon: Reconstruction,
    gap03: Gap03Classification,
    axis: SessionAxis,
) -> dict[str, Any]:
    """The eight fill-derived metrics. ``capital_share`` is computed elsewhere."""

    eligible = {cycle.key for cycle in gap03.eligible_closed}
    unassignable = 0

    # annualized_cycle_count — one window-level observation (GAP-01).
    with contract_context():
        annualized = (
            Decimal(len(gap03.eligible_closed))
            * ANNUALIZATION_CALENDAR_DAYS
            / Decimal(WINDOW_CALENDAR_DAYS)
        )

    # holding_period_sessions — eligible closed cycles only (GAP-03).
    holding: list[Decimal] = []
    holding_excluded = 0
    for cycle in gap03.eligible_closed:
        start = axis.seq(cycle.first_buy_day)
        end = axis.seq(cycle.close_day)
        if start is None or end is None:
            holding_excluded += 1
            unassignable += 1
            continue
        holding.append(Decimal(end - start))

    # adds_per_cycle / add_sizing_multiple / add_interval_sessions.
    adds_per_cycle = [Decimal(cycle.buy_count - 1) for cycle in gap03.eligible_closed]
    add_events = [event for event in recon.adds if event.cycle_key in eligible]
    add_multiples = [event.multiple for event in add_events]
    intervals: list[Decimal] = []
    interval_excluded = 0
    for event in add_events:
        start = axis.seq(event.previous_buy_day)
        end = axis.seq(event.day)
        if start is None or end is None:
            interval_excluded += 1
            unassignable += 1
            continue
        intervals.append(Decimal(end - start))

    # open_lot_age_sessions — every cycle still open at cutoff (GAP-03 keeps
    # right-censored cycles in this family).
    ages: list[Decimal] = []
    age_excluded = 0
    for cycle in gap03.right_censored:
        start = axis.seq(cycle.first_buy_day)
        if start is None:
            age_excluded += 1
            unassignable += 1
            continue
        ages.append(Decimal(axis.cutoff_index - start))

    sells = [event for event in recon.sells if event.cycle_key in eligible]
    excluded_cycles = len(gap03.carry_in_all) + len(gap03.right_censored)
    excluded_add_events = len(recon.adds) - len(add_events)
    excluded_sell_events = len(recon.sells) - len(sells)

    return {
        "annualized_cycle_count": {
            "aggregate_kind": "window_level_rate",
            "aggregate_decimal": decimal_text(annualized),
            "median_decimal": decimal_text(annualized),
            "n": 1,
            "raw_observation_count": 1,
            "excluded_observation_count": 0,
            "closed_cycle_count": len(gap03.eligible_closed),
            "window_calendar_days": WINDOW_CALENDAR_DAYS,
            "note": (
                "GAP-01 calendar-day basis 365.2425/window_calendar_days; "
                "numerator = the GAP-03-eligible closed cycle count"
            ),
        },
        "holding_period_sessions": _observation(
            holding,
            raw=len(gap03.eligible_closed),
            excluded=holding_excluded,
            note="session_gap(first_fill, final_flat) on the sealed 2025 XKRX axis",
        ),
        "add_sizing_multiple": _observation(
            add_multiples,
            raw=len(add_events),
            excluded=excluded_add_events,
            note="add gross notional / the cycle's first filled buy gross notional",
        ),
        "adds_per_cycle": _observation(
            adds_per_cycle,
            raw=len(gap03.eligible_closed),
            excluded=excluded_cycles,
            note=(
                "eligible closed cycles only — GAP-03 removes carry-in and "
                "right-censored cycles from the closed-cycle series; the same "
                "universe is applied to both sides"
            ),
        ),
        "add_interval_sessions": _observation(
            intervals,
            raw=len(add_events),
            excluded=excluded_add_events + interval_excluded,
            note="session_gap(previous buy, add) on the sealed 2025 XKRX axis",
        ),
        "open_lot_age_sessions": _observation(
            ages,
            raw=len(gap03.right_censored),
            excluded=age_excluded,
            note=(
                "cycles still open at cutoff; a carry-in cycle whose first fill "
                "predates the 2025 axis has no session_seq and is excluded, not "
                "clipped"
            ),
        ),
        "trim_share": _observation(
            [event.trim_share for event in sells],
            raw=len(sells),
            excluded=excluded_sell_events,
            note="sell quantity / open quantity immediately before the sell",
        ),
        "signed_realized_pnl_pct": _observation(
            [event.pnl_pct for event in sells],
            raw=len(sells),
            excluded=excluded_sell_events,
            note=(
                "GAP-05 gross-to-gross: 100*(sell price - average buy price)/"
                "average buy price, no fee imputed on either side"
            ),
        ),
        "_census": {
            "closed_cycle_count": len(gap03.eligible_closed),
            "right_censored_count": len(gap03.right_censored),
            "carry_in_excluded_count": len(gap03.carry_in_all),
            "carry_in_excluded_breakdown": {
                "closed_carry_in": sorted(
                    cycle.symbol for cycle in gap03.carry_in_closed
                ),
                "open_carry_in_with_2025_activity": sorted(
                    cycle.symbol for cycle in gap03.carry_in_open_with_activity
                ),
            },
            "carry_in_and_right_censored_overlap": sorted(
                cycle.symbol for cycle in gap03.overlap
            ),
            "session_unassignable_observations": unassignable,
            "anomalies": list(recon.anomalies),
        },
    }


def capital_share_observation(
    daily_ratios: list[Decimal], *, definition_id: str, note: str
) -> dict[str, Any]:
    """GAP-04 locked-share: daily grain, time-weighted mean, p95 and max beside it."""

    if not daily_ratios:
        return {
            "aggregate_kind": "time_weighted_mean",
            "aggregate_decimal": None,
            "median_decimal": None,
            "n": 0,
            "raw_observation_count": 0,
            "excluded_observation_count": 0,
            "capital_share_definition_id": definition_id,
            "note": note,
        }
    if any(value < 0 or value > 1 for value in daily_ratios):
        raise ValueError("locked ratios must be in [0,1]")
    ordered = sorted(daily_ratios)
    with contract_context():
        tw_mean = sum(daily_ratios, Decimal(0)) / Decimal(len(daily_ratios))
    rank = -(-95 * len(ordered) // 100)
    p95 = ordered[rank - 1]
    median_value, _ = median(daily_ratios)
    return {
        "aggregate_kind": "time_weighted_mean",
        "aggregate_decimal": decimal_text(tw_mean),
        "median_decimal": (
            decimal_text(median_value) if median_value is not None else None
        ),
        "n": len(daily_ratios),
        "raw_observation_count": len(daily_ratios),
        "excluded_observation_count": 0,
        "p95_decimal": decimal_text(p95),
        "max_decimal": decimal_text(max(daily_ratios)),
        "capital_share_definition_id": definition_id,
        "note": note,
    }
