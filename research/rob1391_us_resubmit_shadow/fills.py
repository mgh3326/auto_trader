"""Virtual fill models for one resubmitted session (prereg §3).

Conservative (primary) model, per regular-session minute bar that *starts at
or after* the virtual submission instant:

* candidate only if ``bar.low <= P - 1 tick`` (trade-through: the queue at the
  limit is certainly exhausted) AND a valid ask ``<= P`` is observed inside
  that minute at/after submission;
* quantity ``<= floor(1% of that minute's volume)``, whole shares, within the
  remaining Q and within the parent budget;
* price is always P (no gap improvement).

Missing low/volume, missing minute bars, or an unobservable ask stream for a
candidate minute makes the session UNKNOWN from that minute on. UNKNOWN is a
status, not zero shares.

Optimistic model (sensitivity column only): first bar from the submission
minute on with ``low <= P`` fills the whole affordable remainder at P.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from research.rob1391_us_resubmit_shadow import constants as K
from research.rob1391_us_resubmit_shadow.market_data import (
    OK,
    UNKNOWN,
    AskEvidence,
    Bar,
)
from research.rob1391_us_resubmit_shadow.parents import tick_size

MINUTE = timedelta(minutes=1)

AskLookup = Callable[[datetime, datetime], AskEvidence]
Affordable = Callable[[int], int]  # filled-so-far-this-session -> max more shares


@dataclass(frozen=True, slots=True)
class Fill:
    at_utc: datetime
    qty: int
    price: Decimal
    bar_volume: Decimal | None


@dataclass(slots=True)
class SessionFills:
    status: str = OK
    fills: list[Fill] = field(default_factory=list)
    unknown_reason: str | None = None
    unknown_at: datetime | None = None
    ask_digests: list[str] = field(default_factory=list)
    candidate_minutes: int = 0

    @property
    def qty(self) -> int:
        return sum(f.qty for f in self.fills)


def conservative_candidate(bar: Bar, limit: Decimal) -> bool | None:
    """``low <= P - 1 tick``; None when the bar carries no low."""

    if bar.low is None:
        return None
    return bar.low <= limit - tick_size(limit)


def volume_cap(volume: Decimal) -> int:
    return int((volume * K.VOLUME_CAP_FRACTION).to_integral_value(rounding=ROUND_FLOOR))


def eligible_bars(
    bars: tuple[Bar, ...], submit_at: datetime, close_utc: datetime
) -> list[Bar]:
    """Bars fully after submission and fully inside the regular session."""

    return sorted(
        (
            b
            for b in bars
            if b.start_utc >= submit_at and b.start_utc + MINUTE <= close_utc
        ),
        key=lambda b: b.start_utc,
    )


def simulate_conservative(
    *,
    bars: tuple[Bar, ...],
    submit_at: datetime,
    close_utc: datetime,
    limit: Decimal,
    remaining: int,
    affordable: Affordable,
    ask_lookup: AskLookup,
) -> SessionFills:
    out = SessionFills()
    left = remaining
    for bar in eligible_bars(bars, submit_at, close_utc):
        if left <= 0:
            break
        candidate = conservative_candidate(bar, limit)
        if candidate is None or bar.volume is None:
            out.status, out.unknown_reason, out.unknown_at = (
                UNKNOWN,
                "bar_missing_low_or_volume",
                bar.start_utc,
            )
            return out
        if not candidate:
            continue
        out.candidate_minutes += 1
        window_start = max(bar.start_utc, submit_at)
        evidence = ask_lookup(window_start, bar.start_utc + MINUTE)
        out.ask_digests.extend(evidence.digests)
        if evidence.status != OK:
            out.status = UNKNOWN
            out.unknown_reason = f"ask_unobservable:{evidence.reason}"
            out.unknown_at = bar.start_utc
            return out
        if not evidence.found:
            continue
        qty = min(volume_cap(bar.volume), left, affordable(out.qty))
        if qty <= 0:
            continue
        out.fills.append(
            Fill(at_utc=bar.start_utc, qty=qty, price=limit, bar_volume=bar.volume)
        )
        left -= qty
    return out


def simulate_optimistic(
    *,
    bars: tuple[Bar, ...],
    submit_at: datetime,
    close_utc: datetime,
    limit: Decimal,
    remaining: int,
    affordable: Affordable,
) -> SessionFills:
    out = SessionFills()
    submit_minute = submit_at.replace(second=0, microsecond=0)
    for bar in eligible_bars(bars, submit_minute, close_utc):
        if bar.low is None:
            out.status, out.unknown_reason, out.unknown_at = (
                UNKNOWN,
                "bar_missing_low",
                bar.start_utc,
            )
            return out
        if bar.low <= limit:
            qty = min(remaining, affordable(0))
            if qty > 0:
                out.fills.append(
                    Fill(
                        at_utc=bar.start_utc,
                        qty=qty,
                        price=limit,
                        bar_volume=bar.volume,
                    )
                )
            return out
    return out
