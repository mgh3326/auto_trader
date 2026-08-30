"""Pre-registered A/B scoring. Same formula, one scoring_as_of, both arms.

Variant is a label on the sample, never a branch in the arithmetic.
Primary metrics are close-to-entry return and close-peak drawdown.
Sensitivity (window high / window low, and actual-fill return) is always
computed beside primary and must not replace it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Literal

from app.services.buy_gate_ab_shadow.epoch import (
    COLLECTION_EPOCH,
    CollectionEpochMarker,
    assess_collection_readiness,
)
from app.services.buy_gate_ab_shadow.spec import PRE_REGISTRATION

Variant = Literal["A", "B"]
_WINDOWS: tuple[int, ...] = tuple(PRE_REGISTRATION["windows_trading_days"])
PRIMARY_METRICS: tuple[str, ...] = tuple(PRE_REGISTRATION["scoring"]["primary_metrics"])
SENSITIVITY_METRICS: tuple[str, ...] = tuple(
    PRE_REGISTRATION["scoring"]["sensitivity_metrics"]
)


class ScoringError(ValueError):
    """Scoring input violates the pre-registered symmetry contract."""


def _dec(value: object, *, field: str) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ScoringError(f"{field} is not a finite number") from exc
    if not number.is_finite():
        raise ScoringError(f"{field} is not a finite number")
    return number


@dataclass(frozen=True, slots=True)
class DailyBar:
    session_date: date
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ScoringError("high cannot be below low")
        if self.close < self.low or self.close > self.high:
            raise ScoringError("close must lie inside high/low")


@dataclass(frozen=True, slots=True)
class WindowScore:
    window_trading_days: int
    scoreable: bool
    reason: str | None
    simple_return_to_close: Decimal | None
    max_drawdown_from_entry_close_peak: Decimal | None
    simple_return_to_window_high: Decimal | None
    simple_return_to_window_low: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_trading_days": self.window_trading_days,
            "scoreable": self.scoreable,
            "reason": self.reason,
            "primary": {
                "simple_return_to_close": (
                    None
                    if self.simple_return_to_close is None
                    else str(self.simple_return_to_close)
                ),
                "max_drawdown_from_entry_close_peak": (
                    None
                    if self.max_drawdown_from_entry_close_peak is None
                    else str(self.max_drawdown_from_entry_close_peak)
                ),
            },
            "sensitivity": {
                "simple_return_to_window_high": (
                    None
                    if self.simple_return_to_window_high is None
                    else str(self.simple_return_to_window_high)
                ),
                "simple_return_to_window_low": (
                    None
                    if self.simple_return_to_window_low is None
                    else str(self.simple_return_to_window_low)
                ),
            },
        }


def _usable_bars(
    bars: Sequence[DailyBar],
    *,
    decision_date: date,
    scoring_as_of: datetime,
) -> list[DailyBar]:
    if scoring_as_of.tzinfo is None:
        raise ScoringError("scoring_as_of must be timezone-aware")
    cutoff = scoring_as_of.date()
    usable: list[DailyBar] = []
    previous: date | None = None
    for bar in bars:
        if bar.session_date <= decision_date:
            continue
        if bar.session_date > cutoff:
            continue
        if previous is not None and bar.session_date <= previous:
            raise ScoringError("bars must be strictly increasing by session_date")
        previous = bar.session_date
        usable.append(bar)
    return usable


def _simple_return(entry: Decimal, exit_price: Decimal) -> Decimal:
    return (exit_price - entry) / entry


def _max_drawdown_from_entry_close_peak(
    entry: Decimal, closes: Sequence[Decimal]
) -> Decimal:
    peak = entry
    worst = Decimal("0")
    for close in (entry, *closes):
        if close > peak:
            peak = close
        drawdown = (close - peak) / peak
        if drawdown < worst:
            worst = drawdown
    return worst


def score_window(
    *,
    entry: Decimal,
    bars: Sequence[DailyBar],
    decision_date: date,
    scoring_as_of: datetime,
    window_trading_days: int,
) -> WindowScore:
    """Score one sample at one window. Variant is not an argument."""

    if window_trading_days not in _WINDOWS:
        raise ScoringError("window_trading_days is not pre-registered")
    entry_px = _dec(entry, field="entry")
    if entry_px <= 0:
        raise ScoringError("entry must be positive")
    usable = _usable_bars(
        bars, decision_date=decision_date, scoring_as_of=scoring_as_of
    )
    if len(usable) < window_trading_days:
        return WindowScore(
            window_trading_days=window_trading_days,
            scoreable=False,
            reason="insufficient_bars",
            simple_return_to_close=None,
            max_drawdown_from_entry_close_peak=None,
            simple_return_to_window_high=None,
            simple_return_to_window_low=None,
        )
    window = usable[:window_trading_days]
    closes = tuple(bar.close for bar in window)
    highs = tuple(bar.high for bar in window)
    lows = tuple(bar.low for bar in window)
    return WindowScore(
        window_trading_days=window_trading_days,
        scoreable=True,
        reason=None,
        simple_return_to_close=_simple_return(entry_px, window[-1].close),
        max_drawdown_from_entry_close_peak=_max_drawdown_from_entry_close_peak(
            entry_px, closes
        ),
        simple_return_to_window_high=_simple_return(entry_px, max(highs)),
        simple_return_to_window_low=_simple_return(entry_px, min(lows)),
    )


@dataclass(frozen=True, slots=True)
class CohortSample:
    variant: Variant
    symbol: str
    decision_date: date
    entry: Decimal
    entry_basis: Literal["frozen_decision_price"]
    bars: tuple[DailyBar, ...]
    actual_fill_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.entry_basis != "frozen_decision_price":
            raise ScoringError("entry_basis must be frozen_decision_price")


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(median(values)))


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _window_summary(scores: Sequence[WindowScore]) -> dict[str, Any]:
    scoreable = [row for row in scores if row.scoreable]
    returns = [
        row.simple_return_to_close
        for row in scoreable
        if row.simple_return_to_close is not None
    ]
    drawdowns = [
        row.max_drawdown_from_entry_close_peak
        for row in scoreable
        if row.max_drawdown_from_entry_close_peak is not None
    ]
    highs = [
        row.simple_return_to_window_high
        for row in scoreable
        if row.simple_return_to_window_high is not None
    ]
    lows = [
        row.simple_return_to_window_low
        for row in scoreable
        if row.simple_return_to_window_low is not None
    ]
    return {
        "n_submitted": len(scores),
        "n_scoreable": len(scoreable),
        "primary": {
            "mean_simple_return_to_close": _dec_str(_mean(returns)),
            "median_simple_return_to_close": _dec_str(_median(returns)),
            "mean_max_drawdown_from_entry_close_peak": _dec_str(_mean(drawdowns)),
        },
        "sensitivity": {
            "mean_simple_return_to_window_high": _dec_str(_mean(highs)),
            "mean_simple_return_to_window_low": _dec_str(_mean(lows)),
        },
    }


def compare_cohorts(
    samples: Sequence[CohortSample],
    *,
    scoring_as_of: datetime,
    first_valid_record_at: datetime | None = None,
    epoch: CollectionEpochMarker = COLLECTION_EPOCH,
) -> dict[str, Any]:
    """Compare A vs B only after the sealed clock and all events mature."""

    if scoring_as_of.tzinfo is None:
        raise ScoringError("scoring_as_of must be timezone-aware")
    for sample in samples:
        if (
            not epoch.collection_start
            <= sample.decision_date
            < (epoch.collection_end_exclusive)
        ):
            raise ScoringError("sample decision_date is outside the sealed epoch")

    longest_window = max(_WINDOWS)
    events_matured = all(
        len(
            _usable_bars(
                sample.bars,
                decision_date=sample.decision_date,
                scoring_as_of=scoring_as_of,
            )
        )
        >= longest_window
        for sample in samples
    )
    readiness = assess_collection_readiness(
        as_of=scoring_as_of,
        first_valid_record_at=first_valid_record_at,
        event_count=len(samples),
        all_events_matured=events_matured,
        marker=epoch,
    )
    readiness_payload = readiness.as_dict()
    base = {
        "experiment_id": PRE_REGISTRATION["experiment_id"],
        "scoring_as_of": scoring_as_of.isoformat(),
        "collection_armed_at": readiness_payload["collection_armed_at"],
        "collection_start": readiness_payload["collection_start"],
        "collection_end": readiness_payload["collection_last_date"],
        "collection_end_exclusive": readiness_payload["collection_end_exclusive"],
        "collection_complete": readiness.collection_window_closed,
        "collection_window_closed": readiness.collection_window_closed,
        "first_valid_record_at": readiness_payload["first_valid_record_at"],
        "first_valid_record_role": readiness_payload["first_valid_record_role"],
        "event_count": readiness.event_count,
        "all_events_matured": readiness.all_events_matured,
        "scoring_ready": readiness.scoring_ready,
        "status": readiness.status,
        "outcome": readiness.outcome,
        "policy_projection_sha256": epoch.policy_projection_sha256,
        "preregistration_spec_sha256": epoch.preregistration_spec_sha256,
        "policy_implication": "none_until_collection_complete",
        "intermediate_use_forbidden": True,
        "winner_declaration": "forbidden",
        "combine_with": PRE_REGISTRATION["scoring"]["combine_with"],
    }
    if not readiness.scoring_ready:
        # Do not expose intermediate returns or drawdowns for selective reading.
        # Both the collection clock and every event's longest window must close.
        return {
            **base,
            "score_computation": "refused_until_scoring_ready",
        }
    if not samples:
        # A zero-firing epoch still reaches a terminal outcome.  Waiting for a
        # first record here would turn that record into a post-hoc start gate.
        return {
            **base,
            "score_computation": "not_applicable_no_firing",
        }

    by_variant: dict[str, dict[int, list[WindowScore]]] = {
        "A": {window: [] for window in _WINDOWS},
        "B": {window: [] for window in _WINDOWS},
    }
    fill_sensitivity: dict[str, list[Decimal]] = {"A": [], "B": []}
    for sample in samples:
        if sample.variant not in {"A", "B"}:
            raise ScoringError("variant must be A or B")
        for window in _WINDOWS:
            score = score_window(
                entry=sample.entry,
                bars=sample.bars,
                decision_date=sample.decision_date,
                scoring_as_of=scoring_as_of,
                window_trading_days=window,
            )
            by_variant[sample.variant][window].append(score)
        if sample.actual_fill_price is not None:
            fill_sensitivity[sample.variant].append(
                _simple_return(sample.entry, sample.actual_fill_price)
            )

    def _arm(window_scores: dict[int, list[WindowScore]]) -> dict[str, Any]:
        return {
            str(window): _window_summary(scores)
            for window, scores in window_scores.items()
        }

    return {
        **base,
        "score_computation": "completed",
        "primary_metrics": list(PRIMARY_METRICS),
        "sensitivity_metrics": list(SENSITIVITY_METRICS),
        "actual_fill_return_is_sensitivity_only": True,
        "arms": {
            "A": {
                "windows": _arm(by_variant["A"]),
                "sensitivity_actual_fill_vs_frozen_entry": {
                    "n": len(fill_sensitivity["A"]),
                    "mean": (
                        None
                        if _mean(fill_sensitivity["A"]) is None
                        else str(_mean(fill_sensitivity["A"]))
                    ),
                },
            },
            "B": {
                "windows": _arm(by_variant["B"]),
                "sensitivity_actual_fill_vs_frozen_entry": {
                    "n": len(fill_sensitivity["B"]),
                    "mean": (
                        None
                        if _mean(fill_sensitivity["B"]) is None
                        else str(_mean(fill_sensitivity["B"]))
                    ),
                },
            },
        },
    }
