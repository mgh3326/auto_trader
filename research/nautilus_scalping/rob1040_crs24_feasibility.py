"""Pure CRS-24 calendar, event DTOs, and replayed lifecycle accounting.

This module deliberately contains no campaign evaluator or authority issuer.
The only campaign evaluator lives inside a validated closure in the evidence
module; the public empirical entry points below are permanent fail-closed
sentinels for the pre-merge implementation surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from rob944_folds import Fold
from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_contracts import (
    ALL_SIGNAL_OCCUPIED_PER_FOLD,
    ALL_SIGNAL_PLANNED_PER_FOLD,
    CONTRACT_SHA256,
    ENTRY_DELAY_MS,
    FILTER_MANIFEST_SHA256,
    FOLD_HORIZON_CLOSED_PER_FOLD,
    FOLD_SCHEDULE_SHA256,
    HALF_DAY_MS,
    HOLD_MS,
    HORIZON_ELIGIBLE_PER_FOLD,
    MAX_REFERENCE_NOTIONAL_USDT,
    MIN_REFERENCE_NOTIONAL_USDT,
    SCHEDULED_PER_FOLD,
    TARGET_REFERENCE_NOTIONAL_USDT,
    UNIVERSE,
    config_for_id,
    filter_for_symbol,
)
from rob1040_crs24_features import (
    ARBITRATION_STRENGTH_TIE,
    COMMON_MAGNITUDE_GATE_CLOSED,
    COMMON_VOLATILITY_FLOOR,
    DISPERSION_GATE_CLOSED,
    INPUT_HISTORY_INCOMPLETE,
    NO_DIRECTIONAL_CANDIDATE,
    PIT_HISTORY_BELOW_MINIMUM,
    RESIDUAL_VOLATILITY_FLOOR,
    Arbitration,
    CRSFeature,
    DirectionalCandidate,
    PITGateEvaluation,
    arbitrate,
    nearest_rank,
)

from research_contracts.canonical_hash import canonical_sha256

FOLD_HORIZON_CLOSED = "fold_horizon_closed"
ACCOUNT_OCCUPIED = "account_occupied"
ENTRY_REFERENCE_MISSING = "entry_reference_missing"
EXIT_PRESENCE_MISSING = "exit_presence_missing"
ORDER_FILTER_ZERO_QUANTITY = "order_filter_zero_quantity"
ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS = "order_filter_notional_outside_bounds"

CLOSED_REASON_ORDER: tuple[str, ...] = (
    FOLD_HORIZON_CLOSED,
    INPUT_HISTORY_INCOMPLETE,
    RESIDUAL_VOLATILITY_FLOOR,
    COMMON_VOLATILITY_FLOOR,
    PIT_HISTORY_BELOW_MINIMUM,
    DISPERSION_GATE_CLOSED,
    COMMON_MAGNITUDE_GATE_CLOSED,
    NO_DIRECTIONAL_CANDIDATE,
    ARBITRATION_STRENGTH_TIE,
    ACCOUNT_OCCUPIED,
    ENTRY_REFERENCE_MISSING,
    EXIT_PRESENCE_MISSING,
    ORDER_FILTER_ZERO_QUANTITY,
    ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS,
)


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _finite_float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{name} must be built-in float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")
    if value == "0" * 64:
        raise ValueError(f"{name} must not be a zero placeholder")
    return value


class RunAuthorityClosedError(PermissionError):
    """No empirical runner exists before merge/refreeze/separate approval."""


@dataclass(frozen=True, slots=True)
class ReferenceKey:
    symbol: str
    timestamp_ms: int

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or self.symbol not in UNIVERSE:
            raise ValueError("reference key symbol is outside the frozen universe")
        _exact_int(self.timestamp_ms, "timestamp_ms")
        if self.timestamp_ms < 0:
            raise ValueError("reference timestamp must be non-negative")


@dataclass(frozen=True, slots=True)
class EntryReference:
    key: ReferenceKey
    value: Decimal | None

    def __post_init__(self) -> None:
        if type(self.key) is not ReferenceKey:
            raise TypeError("entry key must be exact ReferenceKey")
        if self.value is None:
            return
        if type(self.value) is not Decimal:
            raise TypeError("entry reference value must be exact Decimal or None")
        if not self.value.is_finite() or self.value <= 0:
            raise ValueError("entry reference value must be finite and positive")


@dataclass(frozen=True, slots=True)
class ExitPresence:
    key: ReferenceKey
    present: bool

    def __post_init__(self) -> None:
        if type(self.key) is not ReferenceKey:
            raise TypeError("exit key must be exact ReferenceKey")
        if type(self.present) is not bool:
            raise TypeError("exit presence must be exact bool")


def _registered_fold(fold: Fold) -> Fold:
    if type(fold) is not Fold:
        raise TypeError("fold must be exact rob944_folds.Fold")
    registered = exact_h4_folds()
    if fold.fold_index >= len(registered) or fold != registered[fold.fold_index]:
        raise ValueError("fold is not from exact_h4_folds()")
    return fold


def scheduled_cutoffs(fold: Fold) -> tuple[int, ...]:
    fold = _registered_fold(fold)
    first = ((fold.oos_start_ms + HALF_DAY_MS - 1) // HALF_DAY_MS) * HALF_DAY_MS
    cutoffs = tuple(range(first, fold.oos_end_ms, HALF_DAY_MS))
    if len(cutoffs) != SCHEDULED_PER_FOLD:
        raise ValueError("registered fold does not contain 56 scheduled cutoffs")
    return cutoffs


def is_horizon_eligible(fold: Fold, cutoff_ms: int) -> bool:
    fold = _registered_fold(fold)
    _exact_int(cutoff_ms, "cutoff_ms")
    entry_ts = cutoff_ms + ENTRY_DELAY_MS
    exit_ts = cutoff_ms + HOLD_MS + ENTRY_DELAY_MS
    return (
        fold.oos_start_ms <= entry_ts < fold.oos_end_ms
        and fold.oos_start_ms <= exit_ts < fold.oos_end_ms
    )


def expected_entry_reference_keys() -> tuple[ReferenceKey, ...]:
    return tuple(
        ReferenceKey(symbol, cutoff_ms + ENTRY_DELAY_MS)
        for fold in exact_h4_folds()
        for cutoff_ms in scheduled_cutoffs(fold)
        if is_horizon_eligible(fold, cutoff_ms)
        for symbol in UNIVERSE
    )


def expected_exit_presence_keys() -> tuple[ReferenceKey, ...]:
    return tuple(
        ReferenceKey(symbol, cutoff_ms + HOLD_MS + ENTRY_DELAY_MS)
        for fold in exact_h4_folds()
        for cutoff_ms in scheduled_cutoffs(fold)
        if is_horizon_eligible(fold, cutoff_ms)
        for symbol in UNIVERSE
    )


def reference_domain_sha256() -> str:
    return canonical_sha256(
        {
            "entry": [
                {"symbol": key.symbol, "timestamp_ms": key.timestamp_ms}
                for key in expected_entry_reference_keys()
            ],
            "exit_presence": [
                {"symbol": key.symbol, "timestamp_ms": key.timestamp_ms}
                for key in expected_exit_presence_keys()
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ReferenceSurface:
    """Exact campaign key domain with explicit missing-value/presence sentinels."""

    entries: tuple[EntryReference, ...]
    exit_presence: tuple[ExitPresence, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            type(item) is not EntryReference for item in self.entries
        ):
            raise TypeError("entries must be an exact tuple of EntryReference")
        if tuple(item.key for item in self.entries) != expected_entry_reference_keys():
            raise ValueError("entry references must match the exact frozen key domain")
        if type(self.exit_presence) is not tuple or any(
            type(item) is not ExitPresence for item in self.exit_presence
        ):
            raise TypeError("exit_presence must be an exact tuple of ExitPresence")
        if (
            tuple(item.key for item in self.exit_presence)
            != expected_exit_presence_keys()
        ):
            raise ValueError("exit presence must match the exact frozen key domain")

    @property
    def entry_source_sha256(self) -> str:
        return canonical_sha256(
            [
                {
                    "symbol": item.key.symbol,
                    "timestamp_ms": item.key.timestamp_ms,
                    "value": None if item.value is None else str(item.value),
                }
                for item in self.entries
            ]
        )

    @property
    def exit_presence_source_sha256(self) -> str:
        return canonical_sha256(
            [
                {"symbol": item.key.symbol, "timestamp_ms": item.key.timestamp_ms}
                for item in self.exit_presence
                if item.present
            ]
        )

    def entry_observation(self, key: ReferenceKey) -> EntryReference:
        if type(key) is not ReferenceKey:
            raise TypeError("entry lookup key must be exact ReferenceKey")
        for item in self.entries:
            if item.key == key:
                return item
        raise ValueError("entry lookup escaped the exact frozen key domain")

    def exit_observation(self, key: ReferenceKey) -> ExitPresence:
        if type(key) is not ReferenceKey:
            raise TypeError("exit lookup key must be exact ReferenceKey")
        for item in self.exit_presence:
            if item.key == key:
                return item
        raise ValueError("exit lookup escaped the exact frozen key domain")


@dataclass(frozen=True, slots=True)
class CountBySymbol:
    symbol: str
    count: int

    def __post_init__(self) -> None:
        if self.symbol not in UNIVERSE:
            raise ValueError("symbol count is outside the frozen universe")
        _exact_int(self.count, "count")
        if self.count < 0:
            raise ValueError("symbol count must be non-negative")


@dataclass(frozen=True, slots=True)
class ClosedReasonCount:
    reason: str
    count: int

    def __post_init__(self) -> None:
        if self.reason not in CLOSED_REASON_ORDER:
            raise ValueError("unknown closed reason")
        _exact_int(self.count, "count")
        if self.count < 0:
            raise ValueError("closed count must be non-negative")


@dataclass(frozen=True, slots=True)
class MovementCapacitySummary:
    count: int
    minimum_bp: float | None
    median_bp: float | None
    mean_bp: float | None
    maximum_bp: float | None

    def __post_init__(self) -> None:
        _exact_int(self.count, "count")
        if self.count < 0:
            raise ValueError("movement summary count must be non-negative")
        values = (
            self.minimum_bp,
            self.median_bp,
            self.mean_bp,
            self.maximum_bp,
        )
        if self.count == 0:
            if any(value is not None for value in values):
                raise ValueError("empty movement summary must contain only None")
            return
        if any(value is None for value in values):
            raise ValueError("non-empty movement summary is incomplete")
        complete = tuple(value for value in values if value is not None)
        for value in complete:
            _finite_float(value, "movement capacity value")
            if value < 0:
                raise ValueError("movement capacity values must be non-negative")
        if not (
            complete[0] <= complete[1] <= complete[3]
            and complete[0] <= complete[2] <= complete[3]
        ):
            raise ValueError("movement capacity summary ordering is invalid")


def order_filter_reason(symbol: str, entry_reference: Decimal) -> str | None:
    if type(symbol) is not str or symbol not in UNIVERSE:
        raise ValueError("filter symbol is outside the frozen universe")
    if type(entry_reference) is not Decimal:
        raise TypeError("entry_reference must be exact Decimal")
    if not entry_reference.is_finite() or entry_reference <= 0:
        raise ValueError("entry_reference must be finite and positive")
    fixture = filter_for_symbol(symbol)
    raw_quantity = TARGET_REFERENCE_NOTIONAL_USDT / entry_reference
    step_units = (raw_quantity / fixture.quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    quantity = step_units * fixture.quantity_step
    if quantity <= 0:
        return ORDER_FILTER_ZERO_QUANTITY
    reference_notional = quantity * entry_reference
    if not (
        MIN_REFERENCE_NOTIONAL_USDT <= reference_notional <= MAX_REFERENCE_NOTIONAL_USDT
    ):
        return ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS
    return None


@dataclass(frozen=True, slots=True)
class ScheduledTerminalEvent:
    """One scheduled cutoff and its single, structurally verified terminal."""

    config_id: str
    fold_id: str
    fold_index: int
    cutoff_ms: int
    gate: PITGateEvaluation | None
    arbitration: Arbitration | None
    closed_reason: str | None
    entry_observation: EntryReference | None
    exit_observation: ExitPresence | None
    movement_capacity_bp: float | None

    def __post_init__(self) -> None:
        config_for_id(self.config_id)
        if self.fold_id != f"fold-{self.fold_index:02d}":
            raise ValueError("event fold ID/index mismatch")
        _exact_int(self.cutoff_ms, "cutoff_ms")
        if self.cutoff_ms % HALF_DAY_MS:
            raise ValueError("event cutoff must be 00:00/12:00 UTC")
        if self.closed_reason not in {*CLOSED_REASON_ORDER, None}:
            raise ValueError("event has an unknown terminal reason")
        fold = _registered_fold(exact_h4_folds()[self.fold_index])
        eligible = is_horizon_eligible(fold, self.cutoff_ms)
        if not eligible:
            if (
                self.gate is not None
                or self.arbitration is not None
                or self.closed_reason != FOLD_HORIZON_CLOSED
                or self.entry_observation is not None
                or self.exit_observation is not None
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("fold-horizon event carries downstream state")
            return
        if type(self.gate) is not PITGateEvaluation:
            raise TypeError("eligible event must carry an exact gate evaluation")
        if (
            self.gate.feature.config_id != self.config_id
            or self.gate.feature.cutoff_ms != self.cutoff_ms
        ):
            raise ValueError("event gate identity drifted")
        if not self.gate.joint_pass:
            if (
                self.arbitration is not None
                or self.closed_reason != self.gate.closed_reason
                or self.entry_observation is not None
                or self.exit_observation is not None
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("gate-closed event terminal is not truthful")
            return
        if type(self.gate.feature) is not CRSFeature:
            raise TypeError("joint gate passed without an exact CRSFeature")
        if type(self.arbitration) is not Arbitration:
            raise TypeError("joint-pass event must carry exact arbitration")
        if self.arbitration != arbitrate(self.gate.feature):
            raise ValueError("event arbitration is not feature-derived")
        winner = self.arbitration.winner
        if winner is None:
            if (
                self.closed_reason != self.arbitration.closed_reason
                or self.entry_observation is not None
                or self.exit_observation is not None
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("arbitration-closed event terminal is not truthful")
            return
        if type(winner) is not DirectionalCandidate:
            raise TypeError("event winner must be an exact DirectionalCandidate")
        if self.closed_reason == ACCOUNT_OCCUPIED:
            if (
                self.entry_observation is not None
                or self.exit_observation is not None
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("occupied event must not inspect references")
            return
        expected_entry_key = ReferenceKey(
            winner.symbol,
            self.cutoff_ms + ENTRY_DELAY_MS,
        )
        if (
            type(self.entry_observation) is not EntryReference
            or self.entry_observation.key != expected_entry_key
        ):
            raise ValueError("winner event lacks its exact entry observation")
        if self.entry_observation.value is None:
            if (
                self.closed_reason != ENTRY_REFERENCE_MISSING
                or self.exit_observation is not None
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("entry-missing event terminal is not truthful")
            return
        expected_exit_key = ReferenceKey(
            winner.symbol,
            self.cutoff_ms + HOLD_MS + ENTRY_DELAY_MS,
        )
        if (
            type(self.exit_observation) is not ExitPresence
            or self.exit_observation.key != expected_exit_key
        ):
            raise ValueError("winner event lacks its exact exit observation")
        if not self.exit_observation.present:
            if (
                self.closed_reason != EXIT_PRESENCE_MISSING
                or self.movement_capacity_bp is not None
            ):
                raise ValueError("exit-missing event terminal is not truthful")
            return
        expected_filter_reason = order_filter_reason(
            winner.symbol,
            self.entry_observation.value,
        )
        if self.closed_reason != expected_filter_reason:
            raise ValueError("order-filter/planned terminal is not truthful")
        if self.closed_reason is not None:
            if self.movement_capacity_bp is not None:
                raise ValueError("filter-closed event cannot carry movement capacity")
            return
        expected_movement = self.gate.feature.symbol(winner.symbol).movement_capacity_bp
        _finite_float(self.movement_capacity_bp, "movement_capacity_bp")
        if self.movement_capacity_bp != expected_movement:
            raise ValueError("planned movement capacity is not feature-derived")

    @property
    def planned(self) -> bool:
        return self.closed_reason is None

    @property
    def horizon_eligible(self) -> bool:
        return self.closed_reason != FOLD_HORIZON_CLOSED


@dataclass(frozen=True, slots=True)
class LifecycleReplay:
    """State derived by replaying a complete fold ledger from an empty account."""

    scheduled_count: int
    verified_terminal_count: int
    planned_count: int
    occupied_count: int
    final_active_exit_ts: int | None

    def __post_init__(self) -> None:
        for name in (
            "scheduled_count",
            "verified_terminal_count",
            "planned_count",
            "occupied_count",
        ):
            value = getattr(self, name)
            _exact_int(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.verified_terminal_count > self.scheduled_count:
            raise ValueError("verified terminal count exceeds scheduled rows")
        if self.final_active_exit_ts is not None:
            _exact_int(self.final_active_exit_ts, "final_active_exit_ts")

    @property
    def event_terminal_exact(self) -> bool:
        return self.verified_terminal_count == self.scheduled_count


def replay_lifecycle(
    *,
    config_id: str,
    fold: Fold,
    events: tuple[ScheduledTerminalEvent, ...],
) -> LifecycleReplay:
    """Replay one fold from empty state and reject any state-inconsistent terminal."""
    config_for_id(config_id)
    fold = _registered_fold(fold)
    if type(events) is not tuple or any(
        type(event) is not ScheduledTerminalEvent for event in events
    ):
        raise TypeError("replay events must be exact ScheduledTerminalEvent values")
    if tuple(event.cutoff_ms for event in events) != scheduled_cutoffs(fold):
        raise ValueError("replay requires the exact ordered scheduled key domain")

    active_exit_ts: int | None = None
    verified = 0
    planned = 0
    occupied = 0
    for event in events:
        ScheduledTerminalEvent(
            event.config_id,
            event.fold_id,
            event.fold_index,
            event.cutoff_ms,
            event.gate,
            event.arbitration,
            event.closed_reason,
            event.entry_observation,
            event.exit_observation,
            event.movement_capacity_bp,
        )
        if (
            event.config_id,
            event.fold_id,
            event.fold_index,
        ) != (config_id, fold.fold_id, fold.fold_index):
            raise ValueError("replay event identity drifted")
        if not event.horizon_eligible:
            verified += 1
            continue
        gate = event.gate
        if type(gate) is not PITGateEvaluation:
            raise TypeError("eligible replay event lacks an exact gate")
        if not gate.joint_pass:
            verified += 1
            continue
        arbitration = event.arbitration
        if type(arbitration) is not Arbitration:
            raise TypeError("joint-pass replay event lacks exact arbitration")
        winner = arbitration.winner
        if winner is None:
            verified += 1
            continue

        entry_ts = event.cutoff_ms + ENTRY_DELAY_MS
        should_be_occupied = active_exit_ts is not None and entry_ts <= active_exit_ts
        if should_be_occupied:
            if event.closed_reason != ACCOUNT_OCCUPIED:
                raise ValueError(
                    "lifecycle replay requires ACCOUNT_OCCUPIED while position is active"
                )
            occupied += 1
            verified += 1
            continue
        if event.closed_reason == ACCOUNT_OCCUPIED:
            raise ValueError(
                "lifecycle replay rejects ACCOUNT_OCCUPIED without an active position"
            )
        if event.planned:
            active_exit_ts = event.cutoff_ms + HOLD_MS + ENTRY_DELAY_MS
            planned += 1
        verified += 1

    answer = LifecycleReplay(
        scheduled_count=len(events),
        verified_terminal_count=verified,
        planned_count=planned,
        occupied_count=occupied,
        final_active_exit_ts=active_exit_ts,
    )
    if not answer.event_terminal_exact:
        raise ValueError("lifecycle replay did not verify every scheduled terminal")
    return answer


def _movement_summary(values: list[float]) -> MovementCapacitySummary:
    if not values:
        return MovementCapacitySummary(0, None, None, None, None)
    return MovementCapacitySummary(
        count=len(values),
        minimum_bp=min(values),
        median_bp=nearest_rank(tuple(values), 0.50),
        mean_bp=math.fsum(values) / len(values),
        maximum_bp=max(values),
    )


def _symbol_counts(
    values: dict[str, int],
) -> tuple[CountBySymbol, CountBySymbol, CountBySymbol]:
    return tuple(CountBySymbol(symbol, values[symbol]) for symbol in UNIVERSE)


@dataclass(frozen=True, slots=True)
class CellFeasibility:
    """Event-backed cell; every aggregate is derived and cannot be relabeled."""

    config_id: str
    fold_id: str
    fold_index: int
    contract_sha256: str
    filter_manifest_sha256: str
    fold_schedule_sha256: str
    causal_feature_source_sha256: str
    events: tuple[ScheduledTerminalEvent, ...]

    def __post_init__(self) -> None:
        config_for_id(self.config_id)
        if self.fold_id != f"fold-{self.fold_index:02d}":
            raise ValueError("fold ID/index mismatch")
        fold = _registered_fold(exact_h4_folds()[self.fold_index])
        if self.contract_sha256 != CONTRACT_SHA256:
            raise ValueError("cell contract authority drifted")
        if self.filter_manifest_sha256 != FILTER_MANIFEST_SHA256:
            raise ValueError("cell filter authority drifted")
        if self.fold_schedule_sha256 != FOLD_SCHEDULE_SHA256:
            raise ValueError("cell fold authority drifted")
        _sha256(self.causal_feature_source_sha256, "causal_feature_source_sha256")
        if type(self.events) is not tuple or any(
            type(event) is not ScheduledTerminalEvent for event in self.events
        ):
            raise TypeError("cell events must be exact ScheduledTerminalEvent values")
        if tuple(event.cutoff_ms for event in self.events) != scheduled_cutoffs(fold):
            raise ValueError("cell events must cover the exact scheduled key domain")
        if any(
            (event.config_id, event.fold_id, event.fold_index)
            != (self.config_id, self.fold_id, self.fold_index)
            for event in self.events
        ):
            raise ValueError("cell event identity drifted")
        replay = replay_lifecycle(
            config_id=self.config_id,
            fold=fold,
            events=self.events,
        )
        if not replay.event_terminal_exact:
            raise ValueError("cell lifecycle replay is not terminal-exact")
        if self.scheduled != SCHEDULED_PER_FOLD:
            raise ValueError("cell must contain exactly 56 scheduled events")
        if self.horizon_eligible != HORIZON_ELIGIBLE_PER_FOLD:
            raise ValueError("cell must contain exactly 54 horizon-eligible events")
        if self.fold_horizon_closed != FOLD_HORIZON_CLOSED_PER_FOLD:
            raise ValueError("cell must contain exactly two horizon closes")
        if self.scheduled != self.horizon_eligible + self.fold_horizon_closed:
            raise ValueError("calendar reconciliation failed")
        if self.long_count + self.short_count != self.planned:
            raise ValueError("planned direction counts do not reconcile")
        if self.movement_capacity.count != self.planned:
            raise ValueError("movement capacity must cover every planned event")
        if self.arbitration_winners != (
            self.planned
            + self.occupied
            + self.entry_reference_missing
            + self.exit_presence_missing
            + self.order_filter_closed
        ):
            raise ValueError("winner lifecycle reconciliation failed")
        if sum(item.count for item in self.closed_histogram) + self.planned != (
            self.scheduled
        ):
            raise ValueError("event terminal reconciliation failed")
        if (
            replay.planned_count != self.planned
            or replay.occupied_count != self.occupied
        ):
            raise ValueError("replayed lifecycle counts do not match cell terminals")

    @property
    def lifecycle_replay(self) -> LifecycleReplay:
        return replay_lifecycle(
            config_id=self.config_id,
            fold=exact_h4_folds()[self.fold_index],
            events=self.events,
        )

    @property
    def scheduled(self) -> int:
        return len(self.events)

    @property
    def horizon_eligible(self) -> int:
        return sum(event.horizon_eligible for event in self.events)

    @property
    def valid_input(self) -> int:
        return sum(
            event.gate is not None and type(event.gate.feature) is CRSFeature
            for event in self.events
        )

    @property
    def dispersion_gate_pass(self) -> int:
        return sum(
            event.gate is not None and event.gate.dispersion_pass
            for event in self.events
        )

    @property
    def common_magnitude_gate_pass(self) -> int:
        return sum(
            event.gate is not None and event.gate.common_magnitude_pass
            for event in self.events
        )

    @property
    def joint_gate_pass(self) -> int:
        return sum(
            event.gate is not None and event.gate.joint_pass for event in self.events
        )

    @property
    def directional_candidates(self) -> int:
        return sum(
            0 if event.arbitration is None else len(event.arbitration.candidates)
            for event in self.events
        )

    @property
    def simultaneous_candidate_cutoffs(self) -> int:
        return sum(
            event.arbitration is not None and len(event.arbitration.candidates) == 2
            for event in self.events
        )

    @property
    def arbitration_winners(self) -> int:
        return sum(
            event.arbitration is not None and event.arbitration.winner is not None
            for event in self.events
        )

    @property
    def occupied(self) -> int:
        return sum(event.closed_reason == ACCOUNT_OCCUPIED for event in self.events)

    @property
    def entry_reference_missing(self) -> int:
        return sum(
            event.closed_reason == ENTRY_REFERENCE_MISSING for event in self.events
        )

    @property
    def exit_presence_missing(self) -> int:
        return sum(
            event.closed_reason == EXIT_PRESENCE_MISSING for event in self.events
        )

    @property
    def order_filter_closed(self) -> int:
        return sum(
            event.closed_reason
            in {
                ORDER_FILTER_ZERO_QUANTITY,
                ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS,
            }
            for event in self.events
        )

    @property
    def fold_horizon_closed(self) -> int:
        return sum(event.closed_reason == FOLD_HORIZON_CLOSED for event in self.events)

    @property
    def planned(self) -> int:
        return sum(event.planned for event in self.events)

    @property
    def movement_capacity(self) -> MovementCapacitySummary:
        return _movement_summary(
            [
                event.movement_capacity_bp
                for event in self.events
                if event.movement_capacity_bp is not None
            ]
        )

    def _candidate_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(UNIVERSE, 0)
        for event in self.events:
            if event.arbitration is not None:
                for candidate in event.arbitration.candidates:
                    counts[candidate.symbol] += 1
        return counts

    def _winner_counts(self, *, planned_only: bool) -> dict[str, int]:
        counts = dict.fromkeys(UNIVERSE, 0)
        for event in self.events:
            if (
                event.arbitration is not None
                and event.arbitration.winner is not None
                and (not planned_only or event.planned)
            ):
                counts[event.arbitration.winner.symbol] += 1
        return counts

    @property
    def candidates_by_symbol(
        self,
    ) -> tuple[CountBySymbol, CountBySymbol, CountBySymbol]:
        return _symbol_counts(self._candidate_counts())

    @property
    def winners_by_symbol(
        self,
    ) -> tuple[CountBySymbol, CountBySymbol, CountBySymbol]:
        return _symbol_counts(self._winner_counts(planned_only=False))

    @property
    def planned_by_symbol(
        self,
    ) -> tuple[CountBySymbol, CountBySymbol, CountBySymbol]:
        return _symbol_counts(self._winner_counts(planned_only=True))

    @property
    def maximum_symbol_concentration(self) -> float | None:
        if not self.planned:
            return None
        return max(item.count for item in self.planned_by_symbol) / self.planned

    @property
    def long_count(self) -> int:
        return sum(
            event.planned
            and event.arbitration is not None
            and event.arbitration.winner is not None
            and event.arbitration.winner.side == "LONG"
            for event in self.events
        )

    @property
    def short_count(self) -> int:
        return sum(
            event.planned
            and event.arbitration is not None
            and event.arbitration.winner is not None
            and event.arbitration.winner.side == "SHORT"
            for event in self.events
        )

    @property
    def closed_histogram(self) -> tuple[ClosedReasonCount, ...]:
        counts = dict.fromkeys(CLOSED_REASON_ORDER, 0)
        for event in self.events:
            if event.closed_reason is not None:
                counts[event.closed_reason] += 1
        return tuple(
            ClosedReasonCount(reason, counts[reason]) for reason in CLOSED_REASON_ORDER
        )

    @property
    def consulted_entry_reference_sha256(self) -> str:
        return canonical_sha256(
            [
                {
                    "symbol": event.entry_observation.key.symbol,
                    "timestamp_ms": event.entry_observation.key.timestamp_ms,
                    "value": (
                        None
                        if event.entry_observation.value is None
                        else str(event.entry_observation.value)
                    ),
                }
                for event in self.events
                if event.entry_observation is not None
            ]
        )

    @property
    def consulted_exit_presence_sha256(self) -> str:
        return canonical_sha256(
            [
                {
                    "symbol": event.exit_observation.key.symbol,
                    "timestamp_ms": event.exit_observation.key.timestamp_ms,
                    "present": event.exit_observation.present,
                }
                for event in self.events
                if event.exit_observation is not None
            ]
        )


def synthetic_all_signal_occupancy(fold: Fold) -> tuple[int, int]:
    planned = 0
    occupied = 0
    active_exit_ts: int | None = None
    for cutoff_ms in scheduled_cutoffs(fold):
        if not is_horizon_eligible(fold, cutoff_ms):
            continue
        entry_ts = cutoff_ms + ENTRY_DELAY_MS
        if active_exit_ts is not None and entry_ts <= active_exit_ts:
            occupied += 1
            continue
        planned += 1
        active_exit_ts = cutoff_ms + HOLD_MS + ENTRY_DELAY_MS
    if (
        planned != ALL_SIGNAL_PLANNED_PER_FOLD
        or occupied != ALL_SIGNAL_OCCUPIED_PER_FOLD
    ):
        raise ValueError("synthetic all-signal occupancy authority drifted")
    return planned, occupied


def run_cell(
    *_args: object,
    **_kwargs: object,
) -> None:
    raise RunAuthorityClosedError(
        "empirical cell runner is closed pending merge/refreeze/separate approval"
    )


def run_all_cells(
    *_args: object,
    **_kwargs: object,
) -> None:
    raise RunAuthorityClosedError(
        "empirical campaign runner is closed pending merge/refreeze/separate approval"
    )


__all__ = [
    "ACCOUNT_OCCUPIED",
    "CLOSED_REASON_ORDER",
    "CellFeasibility",
    "ClosedReasonCount",
    "CountBySymbol",
    "ENTRY_REFERENCE_MISSING",
    "EXIT_PRESENCE_MISSING",
    "EntryReference",
    "ExitPresence",
    "FOLD_HORIZON_CLOSED",
    "LifecycleReplay",
    "MovementCapacitySummary",
    "ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS",
    "ORDER_FILTER_ZERO_QUANTITY",
    "ReferenceKey",
    "ReferenceSurface",
    "RunAuthorityClosedError",
    "ScheduledTerminalEvent",
    "expected_entry_reference_keys",
    "expected_exit_presence_keys",
    "is_horizon_eligible",
    "order_filter_reason",
    "replay_lifecycle",
    "reference_domain_sha256",
    "run_all_cells",
    "run_cell",
    "scheduled_cutoffs",
    "synthetic_all_signal_occupancy",
]
