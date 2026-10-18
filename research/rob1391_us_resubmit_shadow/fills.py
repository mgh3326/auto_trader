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
    NS,
    OK,
    UNKNOWN,
    AskEvidence,
    Bar,
    as_ns,
)
from research.rob1391_us_resubmit_shadow.parents import tick_size

MINUTE = timedelta(minutes=1)
MINUTE_NS = 60 * NS

# (window start ns, window end ns) -> evidence; all instants are integer ns
AskLookup = Callable[[int, int], AskEvidence]
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
    bars: tuple[Bar, ...], submit_at: datetime | int, close_utc: datetime | int
) -> list[Bar]:
    """Bars fully after submission and fully inside the regular session
    (compared on integer ns: a submission 1 ns after a minute starts excludes
    that minute)."""

    submit, close = as_ns(submit_at), as_ns(close_utc)
    return sorted(
        (b for b in bars if b.start_ns >= submit and b.start_ns + MINUTE_NS <= close),
        key=lambda b: b.start_ns,
    )


def _duplicate_minutes(bars: tuple[Bar, ...], out: SessionFills) -> bool:
    """Two bars for one minute make that minute's volume ambiguous: the 1 %
    cap would be applied twice. Refuse (UNKNOWN) rather than sum or pick."""

    starts = [b.start_ns for b in bars]
    if len(set(starts)) == len(starts):
        return False
    out.status, out.unknown_reason = UNKNOWN, "duplicate_bar_timestamp"
    return True


def simulate_conservative(
    *,
    bars: tuple[Bar, ...],
    submit_at: datetime | int,
    close_utc: datetime | int,
    limit: Decimal,
    remaining: int,
    affordable: Affordable,
    ask_lookup: AskLookup,
) -> SessionFills:
    out = SessionFills()
    left = remaining
    if _duplicate_minutes(bars, out):
        return out
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
        window_start = max(bar.start_ns, as_ns(submit_at))
        evidence = ask_lookup(window_start, bar.start_ns + MINUTE_NS)
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
    submit_at: datetime | int,
    close_utc: datetime | int,
    limit: Decimal,
    remaining: int,
    affordable: Affordable,
) -> SessionFills:
    out = SessionFills()
    if _duplicate_minutes(bars, out):
        return out
    submit_ns = as_ns(submit_at)
    submit_minute = submit_ns - submit_ns % MINUTE_NS
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
