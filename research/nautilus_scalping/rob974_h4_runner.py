"""ROB-982 H4 stateless PIT phase and exact-entry authority.

These are deliberately narrow pure helpers.  The later adapter supplies the
actual H1 feature function and H2 engine; no mutable engine/indicator state is
accepted or retained here.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from rob944_folds import Fold
from rob974_features import CommonSnapshot, build_complete_4h, compute_common_features
from rob974_features import MinuteBar as H1MinuteBar
from rob974_h2_scenarios import (
    PATH_SCENARIOS,
    S3ScenarioTradeRow,
    S4ScenarioTradeRow,
    build_s3_scenario_ledger,
    build_s4_scenario_ledger,
    s3_ledger_hash,
    s4_ledger_hash,
)
from rob974_h3_h2_adapter import adapt_s3_candidate, adapt_s4_candidate
from rob974_h3_manifest import SYMBOLS
from rob974_h3_s3 import EmitWindow, FeatureContext, S3Candidate
from rob974_h3_s4 import S4Candidate
from rob974_h4_adapter import (
    SealedS3Terminal,
    SealedS4Terminal,
    seal_s3_engine_input,
    seal_s3_engine_output,
    seal_s4_engine_input,
    seal_s4_engine_output,
)
from rob974_h4_contracts import (
    ATTRIBUTION_SCHEMA_VERSION,
    CONTRACT_PROVENANCE,
    MARKET_RETURN_SEMANTIC,
    TERCILE_BINS,
    TERCILE_METHOD,
    H4SourcePins,
)
from rob974_h6a_identity import H6ARowSpec, verify_row_experiment_id
from rob974_h6a_payload import RequiredSourcePins, verify_primary_run_id

from research_contracts.canonical_hash import canonical_sha256

_MINUTE_MS = 60_000
_HEX_CHARS = frozenset("0123456789abcdef")


class H4AttributionError(ValueError):
    """The H4.5 typed attribution seam failed closed."""


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


def run_selected_oos_paths[Winner, Accepted, Outcome](
    *,
    winner: Winner,
    generator: Callable[[Winner], Accepted],
    fresh_engine: Callable[[str], Callable[[Accepted], Outcome]],
) -> tuple[Outcome, Outcome, Outcome]:
    """Generate once, then execute three independently fresh global paths."""
    accepted = generator(winner)
    engines: list[Callable[[Accepted], Outcome]] = []
    outcomes: list[Outcome] = []
    for scenario in ("base13", "primary_stress17", "upward_stress22"):
        engine = fresh_engine(scenario)
        if not callable(engine) or any(engine is prior for prior in engines):
            raise ValueError("each selected OOS scenario requires a fresh engine")
        engines.append(engine)
        outcomes.append(engine(accepted))
    return tuple(outcomes)  # type: ignore[return-value]


def _finite_float(value: object, name: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise H4AttributionError(f"{name} must be an exact finite float")
    return value


def _hex64(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in _HEX_CHARS for char in value)
        or value == "0" * 64
    ):
        raise H4AttributionError(f"{name} must be a non-placeholder lowercase SHA-256")
    return value


def _fold_id(value: object) -> str:
    if type(value) is not str or value not in tuple(f"fold-{i:02d}" for i in range(8)):
        raise H4AttributionError("fold_id must be one of fold-00..fold-07")
    return value


@dataclass(frozen=True, slots=True)
class TercileAssignment:
    complete: bool
    bin_name: str | None
    percentile: float | None
    incomplete_reason: str | None

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise H4AttributionError("tercile assignment complete must be bool")
        if self.complete:
            if self.bin_name not in TERCILE_BINS:
                raise H4AttributionError("complete tercile assignment requires a bin")
            _finite_float(self.percentile, "tercile percentile")
            if not 0.0 <= self.percentile <= 1.0:
                raise H4AttributionError("tercile percentile must lie in [0,1]")
            if self.incomplete_reason is not None:
                raise H4AttributionError(
                    "complete tercile assignment cannot carry a reason"
                )
        elif (
            self.bin_name is not None
            or self.percentile is not None
            or type(self.incomplete_reason) is not str
            or not self.incomplete_reason
        ):
            raise H4AttributionError(
                "incomplete tercile assignment requires null values and a reason"
            )


@dataclass(frozen=True, slots=True)
class TercileAuthority:
    fold_id: str
    train_start_ms: int
    train_end_ms: int
    method: str
    market_return_semantic: str
    reference_points: tuple[tuple[int, float], ...]
    reference_count: int
    reference_hash: str
    complete: bool
    incomplete_reason: str | None

    def __post_init__(self) -> None:
        _fold_id(self.fold_id)
        start = _int(self.train_start_ms, "train_start_ms")
        end = _int(self.train_end_ms, "train_end_ms")
        if start >= end:
            raise H4AttributionError("tercile TRAIN interval must be non-empty")
        if self.method != TERCILE_METHOD:
            raise H4AttributionError("tercile method drift")
        if self.market_return_semantic != MARKET_RETURN_SEMANTIC:
            raise H4AttributionError("tercile market-return semantic drift")
        if type(self.reference_points) is not tuple:
            raise H4AttributionError("tercile reference_points must be a tuple")
        prior: int | None = None
        for point in self.reference_points:
            if type(point) is not tuple or len(point) != 2:
                raise H4AttributionError(
                    "tercile reference point must be (timestamp,M)"
                )
            timestamp, value = point
            timestamp = _int(timestamp, "tercile reference timestamp")
            _finite_float(value, "tercile reference M")
            if not start <= timestamp < end:
                raise H4AttributionError("tercile reference escaped its TRAIN interval")
            if prior is not None and timestamp <= prior:
                raise H4AttributionError("tercile reference timestamps must be ordered")
            prior = timestamp
        if type(self.reference_count) is not int or self.reference_count != len(
            self.reference_points
        ):
            raise H4AttributionError("tercile reference_count mismatch")
        _hex64(self.reference_hash, "tercile reference_hash")
        expected_hash = canonical_sha256(
            {
                "schema_version": "rob974.h4.tercile_authority.v1",
                "fold_id": self.fold_id,
                "train": [start, end],
                "method": self.method,
                "market_return_semantic": self.market_return_semantic,
                "reference_points": [list(point) for point in self.reference_points],
            }
        )
        if self.reference_hash != expected_hash:
            raise H4AttributionError("tercile reference_hash mismatch")
        if type(self.complete) is not bool:
            raise H4AttributionError("tercile authority complete must be bool")
        if self.complete:
            if self.reference_count == 0 or self.incomplete_reason is not None:
                raise H4AttributionError(
                    "complete tercile authority requires TRAIN rows"
                )
        elif self.reference_count != 0 or self.incomplete_reason != (
            "tercile_train_reference_empty"
        ):
            raise H4AttributionError("empty tercile authority must be explicit")


def build_tercile_authority(
    *,
    fold_id: object,
    train_start_ms: object,
    train_end_ms: object,
    snapshots: object,
) -> TercileAuthority:
    fold = _fold_id(fold_id)
    start = _int(train_start_ms, "train_start_ms")
    end = _int(train_end_ms, "train_end_ms")
    if start >= end:
        raise H4AttributionError("tercile TRAIN interval must be non-empty")
    if type(snapshots) is not tuple:
        raise H4AttributionError("TRAIN snapshots must be an exact tuple")
    points: list[tuple[int, float]] = []
    for snapshot in snapshots:
        if type(snapshot) is not CommonSnapshot:
            raise H4AttributionError("TRAIN rows must be exact CommonSnapshot values")
        if not start <= snapshot.decision_ts < end:
            raise H4AttributionError("TRAIN snapshot escaped its fold boundary")
        points.append((snapshot.decision_ts, snapshot.M))
    exact_points = tuple(points)
    payload = {
        "schema_version": "rob974.h4.tercile_authority.v1",
        "fold_id": fold,
        "train": [start, end],
        "method": TERCILE_METHOD,
        "market_return_semantic": MARKET_RETURN_SEMANTIC,
        "reference_points": [list(point) for point in exact_points],
    }
    complete = bool(exact_points)
    return TercileAuthority(
        fold_id=fold,
        train_start_ms=start,
        train_end_ms=end,
        method=TERCILE_METHOD,
        market_return_semantic=MARKET_RETURN_SEMANTIC,
        reference_points=exact_points,
        reference_count=len(exact_points),
        reference_hash=canonical_sha256(payload),
        complete=complete,
        incomplete_reason=None if complete else "tercile_train_reference_empty",
    )


def assign_market_return_tercile(
    authority: object, market_return: object
) -> TercileAssignment:
    if type(authority) is not TercileAuthority:
        raise H4AttributionError("tercile authority must be exact TercileAuthority")
    if not authority.complete:
        return TercileAssignment(
            False, None, None, authority.incomplete_reason or "tercile_incomplete"
        )
    if type(market_return) is not float or not math.isfinite(market_return):
        return TercileAssignment(False, None, None, "tercile_market_return_nonfinite")
    values = tuple(value for _, value in authority.reference_points)
    less = sum(value < market_return for value in values)
    equal = sum(value == market_return for value in values)
    percentile = (less + 0.5 * equal) / len(values)
    if percentile < 1.0 / 3.0:
        bin_name = "lower"
    elif percentile < 2.0 / 3.0:
        bin_name = "middle"
    else:
        bin_name = "top"
    return TercileAssignment(True, bin_name, float(percentile), None)


@dataclass(frozen=True, slots=True)
class AttributionLineage:
    row_spec: H6ARowSpec
    row_id: str
    experiment_id: str
    strategy_key: str
    fold_id: str

    def __post_init__(self) -> None:
        if type(self.row_spec) is not H6ARowSpec:
            raise H4AttributionError("lineage requires exact H6ARowSpec")
        if self.row_id != self.row_spec.row_id:
            raise H4AttributionError("lineage row_id differs from H6-A row")
        if self.experiment_id != self.row_spec.experiment_id:
            raise H4AttributionError("lineage experiment_id differs from H6-A row")
        if self.strategy_key != self.row_spec.strategy_key:
            raise H4AttributionError("lineage strategy_key differs from H6-A row")
        _fold_id(self.fold_id)
        try:
            verify_row_experiment_id(
                self.row_spec, envelope_experiment_id=self.experiment_id
            )
        except (TypeError, ValueError) as exc:
            raise H4AttributionError(
                "lineage experiment_id verification failed"
            ) from exc


def _lineage(row_spec: object, fold_id: object) -> AttributionLineage:
    if type(row_spec) is not H6ARowSpec:
        raise H4AttributionError("row_spec must be exact H6ARowSpec")
    return AttributionLineage(
        row_spec=row_spec,
        row_id=row_spec.row_id,
        experiment_id=row_spec.experiment_id,
        strategy_key=row_spec.strategy_key,
        fold_id=_fold_id(fold_id),
    )


def _scenario_values(row: object) -> None:
    for name in ("funding_bps", "e13_bps", "e17_bps", "e22_bps"):
        _finite_float(getattr(row, name), name)


def _holding_minutes(entry_ts: object, exit_ts: object) -> float:
    entry = _int(entry_ts, "entry_ts")
    exit_ = _int(exit_ts, "exit_ts")
    duration = exit_ - entry
    if duration < 0:
        raise H4AttributionError("realized holding duration must be non-negative")
    if duration % _MINUTE_MS != 0:
        raise H4AttributionError("realized holding duration must align to 60,000ms")
    return duration / float(_MINUTE_MS)


def _row_param(row_spec: H6ARowSpec, name: str) -> float:
    try:
        value = row_spec.components["params"][name]
    except (KeyError, TypeError) as exc:
        raise H4AttributionError(f"H6-A row params missing {name}") from exc
    return _finite_float(value, f"H6-A params.{name}")


@dataclass(frozen=True, slots=True)
class S3SelectedOOSAttribution:
    lineage: AttributionLineage
    candidate: S3Candidate
    scenario_row: S3ScenarioTradeRow
    S: float
    Q: float
    q_min: float
    market_return: float
    market_return_tercile: str
    volatility_percentile: float
    realized_holding_minutes: float
    tp_bps: float
    sl_bps: float

    def __post_init__(self) -> None:
        if type(self.lineage) is not AttributionLineage:
            raise H4AttributionError("S3 row lineage type drift")
        if type(self.candidate) is not S3Candidate:
            raise H4AttributionError("S3 row candidate must be actual H3 type")
        if type(self.scenario_row) is not S3ScenarioTradeRow:
            raise H4AttributionError("S3 row scenario must be actual H2 type")
        trade = self.scenario_row.trade
        if (
            self.lineage.row_id != self.candidate.config_id
            or self.lineage.row_id != trade.config_id
            or self.lineage.fold_id != trade.fold_id
            or self.candidate.decision_ts != trade.signal_ts
            or self.candidate.symbol != trade.symbol
            or self.candidate.side != trade.side
        ):
            raise H4AttributionError("S3 candidate/trade/lineage binding mismatch")
        for name in (
            "S",
            "Q",
            "q_min",
            "market_return",
            "volatility_percentile",
            "realized_holding_minutes",
            "tp_bps",
            "sl_bps",
        ):
            _finite_float(getattr(self, name), f"S3 {name}")
        if (
            self.S != self.candidate.S
            or self.Q != self.candidate.Q
            or self.market_return != self.candidate.market_return_24h
            or self.volatility_percentile != self.candidate.volatility_percentile
            or self.volatility_percentile != trade.volatility_percentile
            or self.tp_bps != self.candidate.d_TP * 10_000.0
            or self.sl_bps != self.candidate.d_SL * 10_000.0
            or self.realized_holding_minutes
            != _holding_minutes(trade.entry_ts, trade.exit_ts)
        ):
            raise H4AttributionError("S3 attribution value drift")
        if self.market_return_tercile not in TERCILE_BINS:
            raise H4AttributionError("S3 tercile outside the closed bins")
        _scenario_values(self.scenario_row)


@dataclass(frozen=True, slots=True)
class S4SelectedOOSAttribution:
    lineage: AttributionLineage
    candidate: S4Candidate
    scenario_row: S4ScenarioTradeRow
    entry_z: float
    entry_z_threshold: float
    D: float
    correlation: float
    half_life: float
    beta_stability: float
    realized_pair_beta: float
    market_return: float
    realized_holding_minutes: float
    tp_bps: float
    sl_bps: float

    def __post_init__(self) -> None:
        if type(self.lineage) is not AttributionLineage:
            raise H4AttributionError("S4 row lineage type drift")
        if type(self.candidate) is not S4Candidate:
            raise H4AttributionError("S4 row candidate must be actual H3 type")
        if type(self.scenario_row) is not S4ScenarioTradeRow:
            raise H4AttributionError("S4 row scenario must be actual H2 type")
        trade = self.scenario_row.trade
        if (
            self.lineage.row_id != self.candidate.config_id
            or self.lineage.row_id != trade.config_id
            or self.lineage.fold_id != trade.fold_id
            or self.candidate.decision_ts != trade.signal_ts
            or (self.candidate.symbol_a, self.candidate.symbol_b) != trade.pair
            or self.candidate.side_a != trade.side_a
            or self.candidate.side_b != trade.side_b
        ):
            raise H4AttributionError("S4 candidate/trade/lineage binding mismatch")
        for name in (
            "entry_z",
            "entry_z_threshold",
            "D",
            "correlation",
            "half_life",
            "beta_stability",
            "realized_pair_beta",
            "market_return",
            "realized_holding_minutes",
            "tp_bps",
            "sl_bps",
        ):
            _finite_float(getattr(self, name), f"S4 {name}")
        side_sign = {"long": 1.0, "short": -1.0}
        expected_beta = (
            side_sign[trade.side_a] * trade.weight_a * trade.beta_a
            + side_sign[trade.side_b] * trade.weight_b * trade.beta_b
        )
        if (
            self.entry_z != self.candidate.observed_z
            or self.entry_z != trade.z_entry
            or self.D != self.candidate.D_bps
            or self.correlation != self.candidate.rho
            or self.half_life != self.candidate.half_life_4h_bars
            or self.beta_stability != self.candidate.beta_stability
            or self.realized_pair_beta != expected_beta
            or self.tp_bps != self.candidate.d_TP * 10_000.0
            or self.sl_bps != self.candidate.d_SL * 10_000.0
            or self.realized_holding_minutes
            != _holding_minutes(trade.entry_ts, trade.exit_ts)
        ):
            raise H4AttributionError("S4 attribution value drift")
        _scenario_values(self.scenario_row)


AttributionRow = S3SelectedOOSAttribution | S4SelectedOOSAttribution


@dataclass(frozen=True, slots=True)
class SelectedOOSPathAttribution:
    strategy: str
    path_scenario: str
    lineage: AttributionLineage
    terminal: SealedS3Terminal | SealedS4Terminal
    engine_input_count: int
    scenario_ledger_hash: str
    rows: tuple[AttributionRow, ...]

    def __post_init__(self) -> None:
        if self.strategy not in ("S3", "S4"):
            raise H4AttributionError("attribution path strategy must be S3 or S4")
        if self.path_scenario not in PATH_SCENARIOS:
            raise H4AttributionError("attribution path scenario drift")
        if type(self.lineage) is not AttributionLineage:
            raise H4AttributionError("attribution path lineage type drift")
        if type(self.engine_input_count) is not int or self.engine_input_count < 0:
            raise H4AttributionError("engine_input_count must be non-negative int")
        _hex64(self.scenario_ledger_hash, "scenario_ledger_hash")
        if type(self.rows) is not tuple:
            raise H4AttributionError("attribution rows must be an exact tuple")
        if self.strategy == "S3":
            if type(self.terminal) is not SealedS3Terminal or any(
                type(row) is not S3SelectedOOSAttribution for row in self.rows
            ):
                raise H4AttributionError("S3 path concrete types drifted")
            expected_ledger = s3_ledger_hash(
                tuple(row.scenario_row for row in self.rows)
            )
            expected_output = seal_s3_engine_output(self.terminal.result)
            trades = tuple(row.scenario_row.trade for row in self.rows)
        else:
            if type(self.terminal) is not SealedS4Terminal or any(
                type(row) is not S4SelectedOOSAttribution for row in self.rows
            ):
                raise H4AttributionError("S4 path concrete types drifted")
            expected_ledger = s4_ledger_hash(
                tuple(row.scenario_row for row in self.rows)
            )
            expected_output = seal_s4_engine_output(self.terminal.result)
            trades = tuple(row.scenario_row.trade for row in self.rows)
        if expected_ledger != self.scenario_ledger_hash:
            raise H4AttributionError("scenario ledger hash mismatch")
        if expected_output != self.terminal.output_seal_sha256:
            raise H4AttributionError("terminal output seal mismatch")
        if trades != self.terminal.result.trades:
            raise H4AttributionError("attribution rows do not equal terminal trades")
        for row in self.rows:
            if (
                row.lineage != self.lineage
                or row.scenario_row.path_scenario != self.path_scenario
            ):
                raise H4AttributionError("path row lineage/scenario mismatch")


def _decision_snapshot_index(value: object) -> dict[int, CommonSnapshot]:
    if type(value) is not tuple:
        raise H4AttributionError("decision_snapshots must be an exact tuple")
    output: dict[int, CommonSnapshot] = {}
    for snapshot in value:
        if type(snapshot) is not CommonSnapshot:
            raise H4AttributionError("decision row must be exact CommonSnapshot")
        if snapshot.decision_ts in output:
            raise H4AttributionError("duplicate decision snapshot")
        output[snapshot.decision_ts] = snapshot
    return output


def bind_s3_attribution_path(
    *,
    row_spec: object,
    fold_id: object,
    path_scenario: object,
    candidates: object,
    terminal: object,
    corpus_end_ts: object,
    horizon_end_ts: object,
    decision_snapshots: object,
    tercile_authority: object,
    funding_lookup=None,
) -> SelectedOOSPathAttribution:
    lineage = _lineage(row_spec, fold_id)
    if not lineage.row_id.startswith("S3-"):
        raise H4AttributionError("S3 path received a non-S3 H6-A row")
    if path_scenario not in PATH_SCENARIOS:
        raise H4AttributionError("S3 path scenario drift")
    if type(candidates) is not tuple or any(
        type(candidate) is not S3Candidate for candidate in candidates
    ):
        raise H4AttributionError("S3 candidates must be exact H3 tuple values")
    if type(terminal) is not SealedS3Terminal:
        raise H4AttributionError("S3 terminal must be exact SealedS3Terminal")
    corpus_end = _int(corpus_end_ts, "corpus_end_ts")
    if horizon_end_ts is not None:
        horizon_end_ts = _int(horizon_end_ts, "horizon_end_ts")
    intents = tuple(
        adapt_s3_candidate(candidate, fold_id=lineage.fold_id)
        for candidate in candidates
    )
    expected_input = seal_s3_engine_input(
        intents, corpus_end_ts=corpus_end, horizon_end_ts=horizon_end_ts
    )
    if expected_input != terminal.input_seal_sha256:
        raise H4AttributionError("S3 terminal input seal mismatch")
    if seal_s3_engine_output(terminal.result) != terminal.output_seal_sha256:
        raise H4AttributionError("S3 terminal output seal mismatch")
    snapshot_by_ts = _decision_snapshot_index(decision_snapshots)
    candidate_by_identity: dict[tuple[str, int], S3Candidate] = {}
    for candidate in candidates:
        if candidate.config_id != lineage.row_id:
            raise H4AttributionError("S3 candidate config differs from H6-A row_id")
        snapshot = snapshot_by_ts.get(candidate.decision_ts)
        if snapshot is None or snapshot.M != candidate.market_return_24h:
            raise H4AttributionError(
                "S3 candidate M differs from decision CommonSnapshot.M"
            )
        identity = (candidate.symbol, candidate.decision_ts)
        if identity in candidate_by_identity:
            raise H4AttributionError("duplicate S3 candidate identity")
        candidate_by_identity[identity] = candidate
    if type(tercile_authority) is not TercileAuthority:
        raise H4AttributionError("S3 requires exact TercileAuthority")
    if tercile_authority.fold_id != lineage.fold_id:
        raise H4AttributionError("S3 tercile authority fold mismatch")
    scenario_rows = build_s3_scenario_ledger(
        terminal.result.trades, path_scenario, funding_lookup
    )
    bound_rows: list[S3SelectedOOSAttribution] = []
    q_min = _row_param(lineage.row_spec, "q_min")
    for scenario_row in scenario_rows:
        trade = scenario_row.trade
        candidate = candidate_by_identity.get((trade.symbol, trade.signal_ts))
        if candidate is None:
            raise H4AttributionError("S3 trade lacks its exact H3 candidate")
        assignment = assign_market_return_tercile(
            tercile_authority, candidate.market_return_24h
        )
        if not assignment.complete or assignment.bin_name is None:
            raise H4AttributionError(
                assignment.incomplete_reason or "S3 tercile assignment incomplete"
            )
        bound_rows.append(
            S3SelectedOOSAttribution(
                lineage=lineage,
                candidate=candidate,
                scenario_row=scenario_row,
                S=candidate.S,
                Q=candidate.Q,
                q_min=q_min,
                market_return=candidate.market_return_24h,
                market_return_tercile=assignment.bin_name,
                volatility_percentile=candidate.volatility_percentile,
                realized_holding_minutes=_holding_minutes(
                    trade.entry_ts, trade.exit_ts
                ),
                tp_bps=candidate.d_TP * 10_000.0,
                sl_bps=candidate.d_SL * 10_000.0,
            )
        )
    exact_rows = tuple(bound_rows)
    return SelectedOOSPathAttribution(
        strategy="S3",
        path_scenario=path_scenario,
        lineage=lineage,
        terminal=terminal,
        engine_input_count=len(intents),
        scenario_ledger_hash=s3_ledger_hash(scenario_rows),
        rows=exact_rows,
    )


def bind_s4_attribution_path(
    *,
    row_spec: object,
    fold_id: object,
    path_scenario: object,
    candidates: object,
    terminal: object,
    corpus_end_ts: object,
    horizon_end_ts: object,
    decision_snapshots: object,
    funding_lookup=None,
) -> SelectedOOSPathAttribution:
    lineage = _lineage(row_spec, fold_id)
    if not lineage.row_id.startswith("S4-"):
        raise H4AttributionError("S4 path received a non-S4 H6-A row")
    if path_scenario not in PATH_SCENARIOS:
        raise H4AttributionError("S4 path scenario drift")
    if type(candidates) is not tuple or any(
        type(candidate) is not S4Candidate for candidate in candidates
    ):
        raise H4AttributionError("S4 candidates must be exact H3 tuple values")
    if type(terminal) is not SealedS4Terminal:
        raise H4AttributionError("S4 terminal must be exact SealedS4Terminal")
    corpus_end = _int(corpus_end_ts, "corpus_end_ts")
    if horizon_end_ts is not None:
        horizon_end_ts = _int(horizon_end_ts, "horizon_end_ts")
    intents = tuple(
        adapt_s4_candidate(candidate, fold_id=lineage.fold_id)
        for candidate in candidates
    )
    expected_input = seal_s4_engine_input(
        intents, corpus_end_ts=corpus_end, horizon_end_ts=horizon_end_ts
    )
    if expected_input != terminal.input_seal_sha256:
        raise H4AttributionError("S4 terminal input seal mismatch")
    if seal_s4_engine_output(terminal.result) != terminal.output_seal_sha256:
        raise H4AttributionError("S4 terminal output seal mismatch")
    snapshot_by_ts = _decision_snapshot_index(decision_snapshots)
    candidate_by_identity: dict[tuple[tuple[str, str], int], S4Candidate] = {}
    for candidate in candidates:
        if candidate.config_id != lineage.row_id:
            raise H4AttributionError("S4 candidate config differs from H6-A row_id")
        if snapshot_by_ts.get(candidate.decision_ts) is None:
            raise H4AttributionError("S4 candidate lacks decision CommonSnapshot")
        identity = ((candidate.symbol_a, candidate.symbol_b), candidate.decision_ts)
        if identity in candidate_by_identity:
            raise H4AttributionError("duplicate S4 candidate identity")
        candidate_by_identity[identity] = candidate
    scenario_rows = build_s4_scenario_ledger(
        terminal.result.trades, path_scenario, funding_lookup
    )
    threshold = _row_param(lineage.row_spec, "z_entry")
    bound_rows: list[S4SelectedOOSAttribution] = []
    for scenario_row in scenario_rows:
        trade = scenario_row.trade
        candidate = candidate_by_identity.get((trade.pair, trade.signal_ts))
        if candidate is None:
            raise H4AttributionError("S4 trade lacks its exact H3 candidate")
        snapshot = snapshot_by_ts[candidate.decision_ts]
        side_sign = {"long": 1.0, "short": -1.0}
        realized_pair_beta = (
            side_sign[trade.side_a] * trade.weight_a * trade.beta_a
            + side_sign[trade.side_b] * trade.weight_b * trade.beta_b
        )
        bound_rows.append(
            S4SelectedOOSAttribution(
                lineage=lineage,
                candidate=candidate,
                scenario_row=scenario_row,
                entry_z=candidate.observed_z,
                entry_z_threshold=threshold,
                D=candidate.D_bps,
                correlation=candidate.rho,
                half_life=candidate.half_life_4h_bars,
                beta_stability=candidate.beta_stability,
                realized_pair_beta=realized_pair_beta,
                market_return=snapshot.M,
                realized_holding_minutes=_holding_minutes(
                    trade.entry_ts, trade.exit_ts
                ),
                tp_bps=candidate.d_TP * 10_000.0,
                sl_bps=candidate.d_SL * 10_000.0,
            )
        )
    exact_rows = tuple(bound_rows)
    return SelectedOOSPathAttribution(
        strategy="S4",
        path_scenario=path_scenario,
        lineage=lineage,
        terminal=terminal,
        engine_input_count=len(intents),
        scenario_ledger_hash=s4_ledger_hash(scenario_rows),
        rows=exact_rows,
    )


def _tercile_payload(authority: TercileAuthority) -> dict[str, object]:
    return {
        "fold_id": authority.fold_id,
        "train": [authority.train_start_ms, authority.train_end_ms],
        "method": authority.method,
        "market_return_semantic": authority.market_return_semantic,
        "reference_count": authority.reference_count,
        "reference_hash": authority.reference_hash,
        "complete": authority.complete,
        "incomplete_reason": authority.incomplete_reason,
    }


def _attribution_row_payload(row: AttributionRow) -> dict[str, object]:
    trade = row.scenario_row.trade
    common: dict[str, object] = {
        "row_id": row.lineage.row_id,
        "experiment_id": row.lineage.experiment_id,
        "strategy_key": row.lineage.strategy_key,
        "fold_id": row.lineage.fold_id,
        "path_scenario": row.scenario_row.path_scenario,
        "signal_ts": trade.signal_ts,
        "entry_ts": trade.entry_ts,
        "exit_ts": trade.exit_ts,
        "exit_reason": trade.exit_reason,
        "gross_bps": trade.gross_bps,
        "e13_bps": row.scenario_row.e13_bps,
        "e17_bps": row.scenario_row.e17_bps,
        "e22_bps": row.scenario_row.e22_bps,
        "realized_holding_minutes": row.realized_holding_minutes,
        "tp_bps": row.tp_bps,
        "sl_bps": row.sl_bps,
        "market_return": row.market_return,
    }
    if type(row) is S3SelectedOOSAttribution:
        return {
            **common,
            "strategy": "S3",
            "symbol": trade.symbol,
            "direction": trade.side,
            "S": row.S,
            "Q": row.Q,
            "q_min": row.q_min,
            "market_return_tercile": row.market_return_tercile,
            "volatility_percentile": row.volatility_percentile,
        }
    return {
        **common,
        "strategy": "S4",
        "pair": list(trade.pair),
        "direction": row.candidate.side,
        "entry_z": row.entry_z,
        "entry_z_threshold": row.entry_z_threshold,
        "D": row.D,
        "correlation": row.correlation,
        "half_life": row.half_life,
        "beta_stability": row.beta_stability,
        "realized_pair_beta": row.realized_pair_beta,
        "gross_notional": trade.gross_notional,
    }


def _path_payload(path: SelectedOOSPathAttribution) -> dict[str, object]:
    return {
        "strategy": path.strategy,
        "row_id": path.lineage.row_id,
        "experiment_id": path.lineage.experiment_id,
        "fold_id": path.lineage.fold_id,
        "path_scenario": path.path_scenario,
        "h2_input_seal_sha256": path.terminal.input_seal_sha256,
        "h2_output_seal_sha256": path.terminal.output_seal_sha256,
        "engine_input_count": path.engine_input_count,
        "scenario_ledger_hash": path.scenario_ledger_hash,
        "rows": [_attribution_row_payload(row) for row in path.rows],
    }


def _envelope_payload(
    *,
    contract_provenance: str,
    full_campaign_hash: str,
    campaign_run_id: str,
    source_pins: RequiredSourcePins,
    h4_source_pins: H4SourcePins,
    paths: tuple[SelectedOOSPathAttribution, ...],
    tercile_authorities: tuple[TercileAuthority, ...],
    deferred_reason: str | None,
) -> dict[str, object]:
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "contract_provenance": contract_provenance,
        "full_campaign_hash": full_campaign_hash,
        "campaign_run_id": campaign_run_id,
        "source_pins": source_pins.as_dict(),
        "h4_source_pins": {
            "runner_bundle_sha256": h4_source_pins.runner_bundle_sha256,
            "pbo_source_sha256": h4_source_pins.pbo_source_sha256,
        },
        "tercile_authorities": [
            _tercile_payload(authority) for authority in tercile_authorities
        ],
        "paths": [_path_payload(path) for path in paths],
        "deferred_reason": deferred_reason,
    }


@dataclass(frozen=True, slots=True)
class SelectedOOSAttributionEnvelope:
    schema_version: str
    contract_provenance: str
    full_campaign_hash: str
    campaign_run_id: str
    source_pins: RequiredSourcePins
    h4_source_pins: H4SourcePins
    paths: tuple[SelectedOOSPathAttribution, ...]
    tercile_authorities: tuple[TercileAuthority, ...]
    deferred_reason: str | None
    producer_seal_sha256: str

    @property
    def rows(self) -> tuple[AttributionRow, ...]:
        return tuple(row for path in self.paths for row in path.rows)

    def __post_init__(self) -> None:
        if self.schema_version != ATTRIBUTION_SCHEMA_VERSION:
            raise H4AttributionError("attribution envelope schema drift")
        if self.contract_provenance not in CONTRACT_PROVENANCE:
            raise H4AttributionError("attribution contract provenance drift")
        _hex64(self.full_campaign_hash, "full_campaign_hash")
        try:
            verify_primary_run_id(
                self.campaign_run_id, full_campaign_hash=self.full_campaign_hash
            )
        except (TypeError, ValueError) as exc:
            raise H4AttributionError("campaign_run_id verification failed") from exc
        if type(self.source_pins) is not RequiredSourcePins:
            raise H4AttributionError("source_pins must be exact RequiredSourcePins")
        if type(self.h4_source_pins) is not H4SourcePins:
            raise H4AttributionError("h4_source_pins must be exact H4SourcePins")
        if type(self.paths) is not tuple or any(
            type(path) is not SelectedOOSPathAttribution for path in self.paths
        ):
            raise H4AttributionError("envelope paths concrete type drift")
        if type(self.tercile_authorities) is not tuple or any(
            type(authority) is not TercileAuthority
            for authority in self.tercile_authorities
        ):
            raise H4AttributionError("envelope tercile authority type drift")
        if len({a.fold_id for a in self.tercile_authorities}) != len(
            self.tercile_authorities
        ):
            raise H4AttributionError("duplicate fold tercile authority")
        if self.contract_provenance == "deferred":
            if self.paths or self.tercile_authorities:
                raise H4AttributionError("deferred attribution forces rows=()")
            if type(self.deferred_reason) is not str or not self.deferred_reason:
                raise H4AttributionError("deferred attribution requires a reason")
        else:
            if self.deferred_reason is not None:
                raise H4AttributionError(
                    "non-deferred attribution cannot carry a reason"
                )
            if not self.paths:
                raise H4AttributionError("non-deferred attribution requires paths")
        if self.contract_provenance in ("actual", "deferred"):
            try:
                self.source_pins.require_production_ready()
            except (TypeError, ValueError) as exc:
                raise H4AttributionError(
                    "production attribution source pins invalid"
                ) from exc
        if self.contract_provenance == "actual":
            groups: dict[tuple[str, str, str], set[str]] = {}
            authority_folds = {
                authority.fold_id for authority in self.tercile_authorities
            }
            for path in self.paths:
                if path.lineage.row_spec.provenance != "production":
                    raise H4AttributionError(
                        "actual attribution requires production row specs"
                    )
                key = (path.strategy, path.lineage.row_id, path.lineage.fold_id)
                groups.setdefault(key, set()).add(path.path_scenario)
                if (
                    path.strategy == "S3"
                    and path.lineage.fold_id not in authority_folds
                ):
                    raise H4AttributionError(
                        "S3 actual path lacks its fold tercile authority"
                    )
            if any(scenarios != set(PATH_SCENARIOS) for scenarios in groups.values()):
                raise H4AttributionError(
                    "actual attribution requires three canonical scenarios per selected unit"
                )
            if len(self.paths) != 3 * len(groups):
                raise H4AttributionError(
                    "actual attribution has duplicate scenario paths"
                )
        expected_seal = canonical_sha256(
            _envelope_payload(
                contract_provenance=self.contract_provenance,
                full_campaign_hash=self.full_campaign_hash,
                campaign_run_id=self.campaign_run_id,
                source_pins=self.source_pins,
                h4_source_pins=self.h4_source_pins,
                paths=self.paths,
                tercile_authorities=self.tercile_authorities,
                deferred_reason=self.deferred_reason,
            )
        )
        if self.producer_seal_sha256 != expected_seal:
            raise H4AttributionError("attribution producer seal mismatch")


def build_actual_attribution_envelope(
    *, plan: object, paths: object, tercile_authorities: object
) -> SelectedOOSAttributionEnvelope:
    from rob974_h4_h6a_adapter import ProductionH4Plan

    if type(plan) is not ProductionH4Plan:
        raise H4AttributionError("actual attribution requires exact ProductionH4Plan")
    if type(paths) is not tuple or any(
        type(path) is not SelectedOOSPathAttribution for path in paths
    ):
        raise H4AttributionError("actual attribution paths must be exact tuple")
    if type(tercile_authorities) is not tuple or any(
        type(authority) is not TercileAuthority for authority in tercile_authorities
    ):
        raise H4AttributionError("actual tercile authorities must be exact tuple")
    plan_by_row = {spec.row_id: spec for spec in plan.row_specs}
    for path in paths:
        if plan_by_row.get(path.lineage.row_id) != path.lineage.row_spec:
            raise H4AttributionError(
                "path lineage is not the exact production-plan row"
            )
    payload = _envelope_payload(
        contract_provenance="actual",
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        source_pins=plan.source_pins,
        h4_source_pins=plan.h4_source_pins,
        paths=paths,
        tercile_authorities=tercile_authorities,
        deferred_reason=None,
    )
    return SelectedOOSAttributionEnvelope(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        contract_provenance="actual",
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        source_pins=plan.source_pins,
        h4_source_pins=plan.h4_source_pins,
        paths=paths,
        tercile_authorities=tercile_authorities,
        deferred_reason=None,
        producer_seal_sha256=canonical_sha256(payload),
    )


def build_deferred_attribution_envelope(
    *, plan: object, reason: object
) -> SelectedOOSAttributionEnvelope:
    from rob974_h4_h6a_adapter import ProductionH4Plan

    if type(plan) is not ProductionH4Plan:
        raise H4AttributionError("deferred attribution requires ProductionH4Plan")
    if type(reason) is not str or not reason:
        raise H4AttributionError("deferred attribution requires a non-empty reason")
    payload = _envelope_payload(
        contract_provenance="deferred",
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        source_pins=plan.source_pins,
        h4_source_pins=plan.h4_source_pins,
        paths=(),
        tercile_authorities=(),
        deferred_reason=reason,
    )
    return SelectedOOSAttributionEnvelope(
        schema_version=ATTRIBUTION_SCHEMA_VERSION,
        contract_provenance="deferred",
        full_campaign_hash=plan.full_campaign_hash,
        campaign_run_id=plan.campaign_run_id,
        source_pins=plan.source_pins,
        h4_source_pins=plan.h4_source_pins,
        paths=(),
        tercile_authorities=(),
        deferred_reason=reason,
        producer_seal_sha256=canonical_sha256(payload),
    )


def validate_attribution_envelope(
    envelope: object,
) -> SelectedOOSAttributionEnvelope:
    if type(envelope) is not SelectedOOSAttributionEnvelope:
        raise H4AttributionError("attribution envelope concrete type drift")
    envelope.__post_init__()
    return envelope


def non_selected_oos_paths() -> tuple[str, str, str]:
    """Typed scenario sentinels; never omit a non-selected fold/config path."""
    return ("not_selected", "not_selected", "not_selected")


__all__ = [
    "AttributionLineage",
    "ExactMinuteEntry",
    "ActualH1PhaseContext",
    "H4AttributionError",
    "H4Phase",
    "S3SelectedOOSAttribution",
    "S4SelectedOOSAttribution",
    "SelectedOOSAttributionEnvelope",
    "SelectedOOSPathAttribution",
    "TercileAssignment",
    "TercileAuthority",
    "assign_market_return_tercile",
    "bind_s3_attribution_path",
    "bind_s4_attribution_path",
    "build_actual_attribution_envelope",
    "candidate_fits_phase",
    "build_actual_h1_phase_context",
    "build_deferred_attribution_envelope",
    "build_tercile_authority",
    "phase_for_fold",
    "phase_horizon_reason",
    "recompute_stateless_phase",
    "resolve_exact_entry",
    "run_selected_oos_paths",
    "validate_attribution_envelope",
    "non_selected_oos_paths",
]
