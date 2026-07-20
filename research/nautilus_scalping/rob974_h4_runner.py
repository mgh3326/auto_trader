"""ROB-982 H4 stateless PIT phase and exact-entry authority.

These are deliberately narrow pure helpers.  The later adapter supplies the
actual H1 feature function and H2 engine; no mutable engine/indicator state is
accepted or retained here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rob944_folds import Fold
from rob974_features import MinuteBar as H1MinuteBar
from rob974_features import build_complete_4h, compute_common_features
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s3 import EmitWindow, FeatureContext


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


@dataclass(frozen=True, slots=True)
class H4Phase:
    name: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.name not in ("train", "selected_oos", "pbo_full_window"):
            raise ValueError("phase is outside H4's closed set")
        if _int(self.start_ms, "start_ms") >= _int(self.end_ms, "end_ms"):
            raise ValueError("phase must be a non-empty half-open interval")

    def contains(self, timestamp_ms: object) -> bool:
        timestamp = _int(timestamp_ms, "timestamp_ms")
        return self.start_ms <= timestamp < self.end_ms


@dataclass(frozen=True, slots=True)
class ExactMinuteEntry:
    open_time_ms: int
    payload: object

    def __post_init__(self) -> None:
        _int(self.open_time_ms, "open_time_ms")


@dataclass(frozen=True, slots=True)
class ActualH1PhaseContext:
    """Fresh merged-H1 result for one phase, with no cross-phase state."""

    phase: H4Phase
    feature_context: FeatureContext
    emit_window: EmitWindow
    phase_snapshot_count: int

    def __post_init__(self) -> None:
        if type(self.phase) is not H4Phase:
            raise TypeError("phase must be exact H4Phase")
        if type(self.feature_context) is not FeatureContext:
            raise TypeError("feature_context must be actual H3 FeatureContext")
        if type(self.emit_window) is not EmitWindow:
            raise TypeError("emit_window must be actual H3 EmitWindow")
        if (self.emit_window.start, self.emit_window.end) != (
            self.phase.start_ms,
            self.phase.end_ms,
        ):
            raise ValueError("emit window must exactly equal the H4 phase")
        if _int(self.phase_snapshot_count, "phase_snapshot_count") < 0:
            raise ValueError("phase_snapshot_count must not be negative")


def build_actual_h1_phase_context(
    *, raw_minutes: object, phase: object
) -> ActualH1PhaseContext:
    """Recompute actual H1 bars/snapshots from raw past data for one phase.

    All supplied data must precede the exclusive phase end.  Thus a future
    sentinel is rejected instead of being silently filtered, while completed
    pre-phase history remains available solely for feature warm-up.  H1's
    complete-only bar and VWAP primitives decide missing-data NO_SIGNAL.
    """
    if type(phase) is not H4Phase:
        raise TypeError("phase must be an exact H4Phase")
    if type(raw_minutes) is not dict:
        raise TypeError("raw_minutes must be a built-in dict")
    if tuple(raw_minutes) != SYMBOLS:
        raise ValueError("raw_minutes must use the exact selected universe/order")
    normalized: dict[str, tuple[H1MinuteBar, ...]] = {}
    for symbol in SYMBOLS:
        rows = raw_minutes[symbol]
        if type(rows) is not tuple:
            raise TypeError("raw minute rows must be built-in tuples")
        prior: int | None = None
        for row in rows:
            if type(row) is not H1MinuteBar:
                raise TypeError("raw minute rows must be actual H1 MinuteBar")
            if row.ts >= phase.end_ms:
                raise ValueError("future minute is outside stateless phase context")
            if prior is not None and row.ts <= prior:
                raise ValueError("raw minute rows must be strictly increasing")
            prior = row.ts
        normalized[symbol] = rows
    bars = {symbol: build_complete_4h(normalized[symbol]) for symbol in SYMBOLS}
    snapshots = tuple(
        snapshot
        for snapshot in compute_common_features(normalized)
        if snapshot.decision_ts < phase.end_ms
    )
    context = FeatureContext.from_h1(bars, snapshots)
    count = sum(phase.contains(snapshot.decision_ts) for snapshot in snapshots)
    return ActualH1PhaseContext(
        phase,
        context,
        EmitWindow(phase.start_ms, phase.end_ms),
        count,
    )


def phase_for_fold(fold: object, phase: object) -> H4Phase:
    if type(fold) is not Fold:
        raise TypeError("fold must be an exact ROB-944 Fold")
    if phase == "train":
        return H4Phase("train", fold.train_start_ms, fold.train_end_ms)
    if phase == "selected_oos":
        return H4Phase("selected_oos", fold.oos_start_ms, fold.oos_end_ms)
    raise ValueError("fold phases are train or selected_oos")


def candidate_fits_phase(
    *, signal_ts: object, max_hold_ms: object, phase_end_ms: object
) -> bool:
    """Horizon equality is valid; no position may be truncated across a phase."""
    signal = _int(signal_ts, "signal_ts")
    hold = _int(max_hold_ms, "max_hold_ms")
    end = _int(phase_end_ms, "phase_end_ms")
    if hold < 0:
        raise ValueError("max_hold_ms must not be negative")
    return signal + hold <= end


def phase_horizon_reason(phase: object) -> str:
    if phase == "train":
        return "insufficient_train_exit_horizon"
    if phase == "selected_oos":
        return "insufficient_oos_exit_horizon"
    if phase == "pbo_full_window":
        return "insufficient_pbo_exit_horizon"
    raise ValueError("phase outside H4's closed horizon taxonomy")


def resolve_exact_entry(
    *, decision_close_ms: object, minutes: object
) -> ExactMinuteEntry | None:
    """Return only the contiguous next one-minute open; never scan forward."""
    decision = _int(decision_close_ms, "decision_close_ms")
    if type(minutes) is not tuple:
        raise TypeError("minutes must be a built-in tuple")
    if not minutes:
        return None
    first = minutes[0]
    if type(first) is not ExactMinuteEntry:
        raise TypeError("minutes must contain exact ExactMinuteEntry values")
    # The caller may provide later rows for engine context.  H4 intentionally
    # observes only the first candidate tick so missing exact data is NO_TRADE.
    return first if first.open_time_ms == decision else None


def recompute_stateless_phase[T](
    *,
    raw_past_context: object,
    phase: object,
    feature_builder: Callable[[object, H4Phase], T],
) -> T:
    """Invoke a fresh actual-H1-compatible builder with feature-only context.

    The builder receives no previous result, engine, position, cooldown, day
    state, capture sink, or diagnostics carrier, making carry-over impossible
    at this H4 boundary.  It must itself reject future/incomplete raw inputs.
    """
    if type(phase) is not H4Phase:
        raise TypeError("phase must be an exact H4Phase")
    if not callable(feature_builder):
        raise TypeError("feature_builder must be callable")
    return feature_builder(raw_past_context, phase)


__all__ = [
    "ExactMinuteEntry",
    "ActualH1PhaseContext",
    "H4Phase",
    "candidate_fits_phase",
    "build_actual_h1_phase_context",
    "phase_for_fold",
    "phase_horizon_reason",
    "recompute_stateless_phase",
    "resolve_exact_entry",
]
