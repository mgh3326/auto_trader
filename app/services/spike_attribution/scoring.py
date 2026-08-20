"""Hook ⓑ — pre-registered follow-through scoring (pure).

The question is "does a spike of type X still hold N days later?". The formula,
the windows, the verdict bins, and the sample floor are all pinned in
:mod:`app.services.spike_attribution.spec` *before* any event is scored, so a
disappointing result cannot be re-read favourably afterwards.

The retention ratio measures how much of the spike move survived::

    ratio = (close_at_window_end - prev_close) / (spike_close - prev_close)

The denominator carries the direction, so a -7% crash and a +7% pop score on
the same expression: ``1.0`` means the move fully held, ``0.0`` means it was
entirely given back, negative means it reversed through the pre-spike close.

Missing bars are never imputed. A window that does not have its full count of
trading bars is ``unscorable``, and that is reported as its own class rather
than folded into ``faded``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.spike_attribution.attribute import scored_class
from app.services.spike_attribution.contract import DailyBar, SpikeAttribution
from app.services.spike_attribution.spec import PRE_REGISTRATION

_FT = PRE_REGISTRATION["follow_through"]

WINDOWS_TRADING_DAYS: tuple[int, ...] = tuple(_FT["windows_trading_days"])
MIN_EVENTS_PER_TYPE: int = int(_FT["min_events_per_type_for_comparison"])

VERDICT_EXTENDED = "extended"
VERDICT_RETAINED = "retained"
VERDICT_FADED = "faded"
VERDICT_REVERSED = "reversed"
VERDICT_UNSCORABLE = "unscorable"

_EXTENDED_MIN = Decimal("1.0")
_RETAINED_MIN = Decimal("0.5")
_FADED_MIN = Decimal("0.0")


class ScoringError(ValueError):
    """Raised when scoring inputs cannot yield a deterministic verdict."""


@dataclass(frozen=True)
class FollowThroughScore:
    symbol: str
    session_date: str
    scored_class: str
    window_trading_days: int
    verdict: str
    retention_ratio: Decimal | None
    max_favorable_excursion_ratio: Decimal | None
    max_adverse_excursion_ratio: Decimal | None
    bars_used: int
    unscorable_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date,
            "scored_class": self.scored_class,
            "window_trading_days": self.window_trading_days,
            "verdict": self.verdict,
            "retention_ratio": (
                str(self.retention_ratio) if self.retention_ratio is not None else None
            ),
            "max_favorable_excursion_ratio": (
                str(self.max_favorable_excursion_ratio)
                if self.max_favorable_excursion_ratio is not None
                else None
            ),
            "max_adverse_excursion_ratio": (
                str(self.max_adverse_excursion_ratio)
                if self.max_adverse_excursion_ratio is not None
                else None
            ),
            "bars_used": self.bars_used,
            "unscorable_reason": self.unscorable_reason,
        }


def classify_ratio(ratio: Decimal) -> str:
    if ratio >= _EXTENDED_MIN:
        return VERDICT_EXTENDED
    if ratio >= _RETAINED_MIN:
        return VERDICT_RETAINED
    if ratio >= _FADED_MIN:
        return VERDICT_FADED
    return VERDICT_REVERSED


def _ratio(value: Decimal, prev_close: Decimal, denominator: Decimal) -> Decimal:
    return ((value - prev_close) / denominator).quantize(Decimal("0.0001"))


def score_event(
    *,
    attribution: SpikeAttribution,
    subsequent_bars: list[DailyBar],
    window_trading_days: int,
) -> FollowThroughScore:
    """Score one event over one pinned window.

    ``subsequent_bars`` are the bars strictly *after* the spike session, in
    ascending date order. Only the first ``window_trading_days`` of them are
    read; anything past the scoring as-of is ignored by construction.
    """

    if window_trading_days not in WINDOWS_TRADING_DAYS:
        raise ScoringError(
            f"window {window_trading_days} is not pre-registered "
            f"{list(WINDOWS_TRADING_DAYS)}"
        )
    event = attribution.event
    denominator = event.close - event.prev_close
    klass = scored_class(attribution)
    base = {
        "symbol": event.symbol,
        "session_date": event.session_date.isoformat(),
        "scored_class": klass,
        "window_trading_days": window_trading_days,
    }

    if denominator == 0:
        # A spike that closed flat has no move to retain. It cannot be scored on
        # this formula, and inventing a denominator would fabricate a verdict.
        return FollowThroughScore(
            **base,
            verdict=VERDICT_UNSCORABLE,
            retention_ratio=None,
            max_favorable_excursion_ratio=None,
            max_adverse_excursion_ratio=None,
            bars_used=0,
            unscorable_reason="zero_denominator_close_equals_prev_close",
        )

    ordered = sorted(subsequent_bars, key=lambda row: row.session_date)
    ordered = [row for row in ordered if row.session_date > event.session_date]
    window = ordered[:window_trading_days]
    if len(window) < window_trading_days:
        return FollowThroughScore(
            **base,
            verdict=VERDICT_UNSCORABLE,
            retention_ratio=None,
            max_favorable_excursion_ratio=None,
            max_adverse_excursion_ratio=None,
            bars_used=len(window),
            unscorable_reason=(
                f"insufficient_bars_{len(window)}_of_{window_trading_days}"
            ),
        )

    ratio = _ratio(window[-1].close, event.prev_close, denominator)
    excursions = [_ratio(row.high, event.prev_close, denominator) for row in window] + [
        _ratio(row.low, event.prev_close, denominator) for row in window
    ]
    return FollowThroughScore(
        **base,
        verdict=classify_ratio(ratio),
        retention_ratio=ratio,
        max_favorable_excursion_ratio=max(excursions),
        max_adverse_excursion_ratio=min(excursions),
        bars_used=len(window),
        unscorable_reason=None,
    )


def aggregate_by_class(scores: list[FollowThroughScore]) -> dict[str, Any]:
    """Per-class counts, gated by the pre-registered sample floor.

    Below the floor the aggregate reports counts and nothing else: no mean, no
    ranking, no "type X holds better". That comparison is what the floor exists
    to withhold.
    """

    by_class: dict[str, dict[str, Any]] = {}
    for score in scores:
        bucket = by_class.setdefault(
            score.scored_class,
            {"n": 0, "n_scorable": 0, "verdicts": {}, "windows": {}},
        )
        bucket["n"] += 1
        bucket["verdicts"][score.verdict] = bucket["verdicts"].get(score.verdict, 0) + 1
        key = f"{score.window_trading_days}d"
        bucket["windows"][key] = bucket["windows"].get(key, 0) + 1
        if score.verdict != VERDICT_UNSCORABLE:
            bucket["n_scorable"] += 1

    for bucket in by_class.values():
        bucket["meets_comparison_floor"] = bucket["n_scorable"] >= MIN_EVENTS_PER_TYPE

    floor_met = all(bucket["meets_comparison_floor"] for bucket in by_class.values())
    return {
        "min_events_per_type_for_comparison": MIN_EVENTS_PER_TYPE,
        "by_class": by_class,
        "cross_class_comparison_allowed": bool(by_class) and floor_met,
        "below_floor_disposition": _FT["below_floor_disposition"],
        "winner_declaration": "forbidden_until_floor_met",
    }


__all__ = [
    "MIN_EVENTS_PER_TYPE",
    "VERDICT_EXTENDED",
    "VERDICT_FADED",
    "VERDICT_RETAINED",
    "VERDICT_REVERSED",
    "VERDICT_UNSCORABLE",
    "WINDOWS_TRADING_DAYS",
    "FollowThroughScore",
    "ScoringError",
    "aggregate_by_class",
    "classify_ratio",
    "score_event",
]
