"""Offline H5 weekly scoring. Both lanes include the same fee assumption."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from .control import ActualTrip, ControlTrip

_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class NavSample:
    observed_at: dt.datetime
    nav_usdt: Decimal


@dataclass(frozen=True)
class WeeklyLine:
    week: str
    actual_trips: int
    control_trips: int
    actual_pf: Decimal
    control_pf: Decimal
    actual_net_usdt: Decimal
    control_net_usdt: Decimal
    actual_fees_usdt: Decimal
    control_fees_usdt: Decimal
    actual_mdd: Decimal | None
    control_mdd: Decimal | None


@dataclass(frozen=True)
class H5ScoreCard:
    label: str
    weeks_elapsed: Decimal
    actual_trip_count: int
    control_trip_count: int
    actual_pf: Decimal
    control_pf: Decimal
    mdd: Decimal | None
    annualized_net_return: Decimal | None
    operating_cost_note: str
    control_scope_note: str
    weekly: tuple[WeeklyLine, ...]


def profit_factor(pnls: list[Decimal]) -> Decimal:
    wins = sum((pnl for pnl in pnls if pnl > 0), Decimal(0))
    losses = -sum((pnl for pnl in pnls if pnl < 0), Decimal(0))
    if losses == 0:
        return Decimal("Infinity") if wins > 0 else Decimal(0)
    return wins / losses


def max_drawdown(nav_samples: list[NavSample]) -> Decimal | None:
    if not nav_samples:
        return None
    peak = Decimal(0)
    worst = Decimal(0)
    for sample in sorted(nav_samples, key=lambda row: row.observed_at):
        if sample.nav_usdt <= 0 or not sample.nav_usdt.is_finite():
            return None
        peak = max(peak, sample.nav_usdt)
        worst = max(worst, (peak - sample.nav_usdt) / peak)
    return worst


def _week(value: dt.datetime) -> str:
    iso = value.astimezone(_KST).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def score_h5(
    *,
    actual: list[ActualTrip],
    control: list[ControlTrip] | None,
    nav_samples: list[NavSample],
    t0: dt.datetime,
    as_of: dt.datetime,
) -> H5ScoreCard:
    if t0.tzinfo is None or as_of.tzinfo is None or as_of < t0:
        raise ValueError("aware ordered H5 scoring window required")
    actual = [row for row in actual if t0 <= row.opened_at <= row.closed_at <= as_of]
    control = (
        [row for row in control if t0 <= row.opened_at <= row.closed_at <= as_of]
        if control is not None
        else None
    )
    nav_samples = [row for row in nav_samples if t0 <= row.observed_at <= as_of]
    weeks = Decimal(str((as_of - t0).total_seconds())) / Decimal(7 * 86400)
    actual_pf = profit_factor([row.net_pnl_usdt for row in actual])
    control_pf = (
        profit_factor([row.net_pnl_usdt for row in control])
        if control is not None
        else Decimal(0)
    )
    mdd = max_drawdown(nav_samples)
    control_count = len(control) if control is not None else 0
    if mdd is not None and mdd > Decimal("0.15"):
        label = "FAIL-RISK"
    elif len(actual) >= 30 and actual_pf < Decimal("0.8"):
        label = "FAIL-RISK"
    elif (
        weeks < 8
        or len(actual) < 60
        or control is None
        or control_count != len(actual)
        or {row.signal_key for row in control} != {row.signal_key for row in actual}
        or mdd is None
    ):
        label = "INSUFFICIENT_SAMPLE"
    elif (
        actual_pf >= Decimal("1.2")
        and mdd <= Decimal("0.15")
        and actual_pf > control_pf
    ):
        label = "PASS"
    else:
        label = "FAIL-EFFICACY"

    annualized: Decimal | None = None
    if len(nav_samples) >= 2 and as_of > t0:
        ordered = sorted(nav_samples, key=lambda row: row.observed_at)
        start_nav = ordered[0].nav_usdt
        end_nav = ordered[-1].nav_usdt
        if start_nav > 0 and end_nav > 0:
            days = (as_of - t0).total_seconds() / 86400
            annualized = Decimal(str((float(end_nav / start_nav) ** (365 / days)) - 1))
    if annualized is None:
        cost_note = (
            "Annualized net return unavailable; weekly operating cost cannot be judged."
        )
    elif annualized < Decimal("0.10"):
        cost_note = "Annualized net return below 10%; weekly review operating cost may exceed benefit."
    else:
        cost_note = (
            "Annualized net return at least 10%; operating cost still requires review."
        )

    weeks_seen = sorted(
        {_week(row.closed_at) for row in actual}
        | ({_week(row.closed_at) for row in control} if control else set())
        | {_week(row.observed_at) for row in nav_samples}
    )
    initial_nav = (
        min(nav_samples, key=lambda row: row.observed_at).nav_usdt
        if nav_samples
        else None
    )
    control_nav: list[NavSample] = []
    if initial_nav is not None:
        equity = initial_nav
        control_nav.append(NavSample(t0, equity))
        for row in sorted(control or [], key=lambda row: row.closed_at):
            equity += row.net_pnl_usdt
            control_nav.append(NavSample(row.closed_at, equity))

    def weekly_mdd(samples: list[NavSample], week: str) -> Decimal | None:
        inside = sorted(
            [row for row in samples if _week(row.observed_at) == week],
            key=lambda row: row.observed_at,
        )
        if not inside:
            return None
        previous = [row for row in samples if row.observed_at < inside[0].observed_at]
        if previous:
            inside.insert(0, max(previous, key=lambda row: row.observed_at))
        return max_drawdown(inside)

    lines: list[WeeklyLine] = []
    for week in weeks_seen:
        a = [row for row in actual if _week(row.closed_at) == week]
        c = [row for row in control or [] if _week(row.closed_at) == week]
        lines.append(
            WeeklyLine(
                week=week,
                actual_trips=len(a),
                control_trips=len(c),
                actual_pf=profit_factor([row.net_pnl_usdt for row in a]),
                control_pf=profit_factor([row.net_pnl_usdt for row in c]),
                actual_net_usdt=sum((row.net_pnl_usdt for row in a), Decimal(0)),
                control_net_usdt=sum((row.net_pnl_usdt for row in c), Decimal(0)),
                actual_fees_usdt=sum((row.fees_usdt for row in a), Decimal(0)),
                control_fees_usdt=sum((row.fees_usdt for row in c), Decimal(0)),
                actual_mdd=weekly_mdd(nav_samples, week),
                control_mdd=weekly_mdd(control_nav, week),
            )
        )
    return H5ScoreCard(
        label=label,
        weeks_elapsed=weeks,
        actual_trip_count=len(actual),
        control_trip_count=control_count,
        actual_pf=actual_pf,
        control_pf=control_pf,
        mdd=mdd,
        annualized_net_return=annualized,
        operating_cost_note=cost_note,
        control_scope_note="Random control isolates entry-signal value, not envelope value.",
        weekly=tuple(lines),
    )
