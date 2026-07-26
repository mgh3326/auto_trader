"""Capability-bound, event-replayed CRS-24 synthetic feasibility evidence."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Protocol

from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_contracts import (
    ACTIVE_CONFIGS,
    CONTRACT_SHA256,
    ENTRY_DELAY_MS,
    FILTER_MANIFEST_SHA256,
    FOLD_SCHEDULE_SHA256,
    HOLD_MS,
    PREREGISTRATION_SHA256,
    UNIVERSE,
)
from rob1040_crs24_feasibility import (
    ACCOUNT_OCCUPIED,
    CLOSED_REASON_ORDER,
    ENTRY_REFERENCE_MISSING,
    EXIT_PRESENCE_MISSING,
    FOLD_HORIZON_CLOSED,
    ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS,
    ORDER_FILTER_ZERO_QUANTITY,
    CellFeasibility,
    ReferenceKey,
    ReferenceSurface,
    RunAuthorityClosedError,
    ScheduledTerminalEvent,
    is_horizon_eligible,
    order_filter_reason,
    scheduled_cutoffs,
)
from rob1040_crs24_features import (
    CRSFeature,
    CRSFeatureGenerator,
    arbitrate,
    nearest_rank,
)
from rob1040_crs24_synthetic import (
    SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256,
    SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256,
    SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256,
    SYNTHETIC_FIXTURE_CONTENT_SHA256,
    SYNTHETIC_FIXTURE_VERSION,
    build_synthetic_fixture,
    fixture_content_sha256,
    validate_frozen_synthetic_fixture,
)

from research_contracts.canonical_hash import canonical_sha256

EVIDENCE_SCHEMA_VERSION = "rob1040.crs24.corr1.feasibility_evidence.v3"
FROZEN_SYNTHETIC_EVIDENCE_SHA256 = (
    "ef9b0b819bef5e4cb0a63a9f88b5df0a7a65832d103d4ed379ba460fa3232bab"
)
# Mandatory `extra_authority` key for the `refrozen_real_corpus` posture: the
# real corpus has no compile-time content pin, so its provenance trail must at
# minimum name the frozen corpus manifest it was loaded from.
REAL_CORPUS_MANIFEST_AUTHORITY_KEY = "corpus_manifest_content_sha256"


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def _sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _symbol_counts(values: object) -> dict[str, int]:
    return {item.symbol: item.count for item in values}


def _movement_capacity_values_bp(cell: CellFeasibility) -> list[float]:
    return sorted(
        event.movement_capacity_bp
        for event in cell.events
        if event.movement_capacity_bp is not None
    )


def _movement_payload(cell: CellFeasibility) -> dict[str, object]:
    summary = cell.movement_capacity
    return {
        "unit": "bp",
        "count": summary.count,
        "minimum": summary.minimum_bp,
        "median": summary.median_bp,
        "mean": summary.mean_bp,
        "maximum": summary.maximum_bp,
        "values_bp": _movement_capacity_values_bp(cell),
        "posture": "trailing_only_diagnostic",
    }


def _cell_core_payload(cell: CellFeasibility) -> dict[str, object]:
    closed = {item.reason: item.count for item in cell.closed_histogram}
    closed_total = sum(closed.values())
    replay = cell.lifecycle_replay
    payload = {
        "cell": {
            "config_id": cell.config_id,
            "fold_id": cell.fold_id,
            "fold_index": cell.fold_index,
        },
        "hashes": {
            "preregistration_sha256": PREREGISTRATION_SHA256,
            "contract_sha256": cell.contract_sha256,
            "filter_manifest_sha256": cell.filter_manifest_sha256,
            "fold_schedule_sha256": cell.fold_schedule_sha256,
            "causal_feature_source_sha256": cell.causal_feature_source_sha256,
            "consulted_entry_reference_sha256": (cell.consulted_entry_reference_sha256),
            "consulted_exit_presence_sha256": (cell.consulted_exit_presence_sha256),
        },
        "authority": {
            "complete_4h": "rob974_features.Bar4h/build_complete_4h",
            "calendar": "rob974_h4_contracts.exact_h4_folds/rob944_folds",
            "quantiles": "nearest_rank_prior_60_calendar_days",
            "filter": "rob1040_local_static_fixture",
            "exit_reference": "timestamp_presence_only",
            "numeric_hash_scope": "causal_feature_and_consulted_entry_only",
            "campaign_binding": "validated_context_object_identity",
        },
        "calendar_counts": {
            "scheduled": cell.scheduled,
            "horizon_eligible": cell.horizon_eligible,
            "fold_horizon_closed": cell.fold_horizon_closed,
        },
        "valid_input": cell.valid_input,
        "individual_gates": {
            "dispersion_pass": cell.dispersion_gate_pass,
            "common_magnitude_pass": cell.common_magnitude_gate_pass,
        },
        "joint_gate_pass": cell.joint_gate_pass,
        "symbol_candidates": {
            "directional_total": cell.directional_candidates,
            "simultaneous_cutoffs": cell.simultaneous_candidate_cutoffs,
            "by_symbol": _symbol_counts(cell.candidates_by_symbol),
        },
        "arbitration": {
            "winners": cell.arbitration_winners,
            "by_symbol": _symbol_counts(cell.winners_by_symbol),
        },
        "occupancy": {"closed": cell.occupied},
        "reference_availability": {
            "entry_missing": cell.entry_reference_missing,
            "exit_presence_missing": cell.exit_presence_missing,
        },
        "order_filter": {"closed": cell.order_filter_closed},
        "planned": cell.planned,
        "movement_capacity": _movement_payload(cell),
        "symbol_concentration": {
            "planned_by_symbol": _symbol_counts(cell.planned_by_symbol),
            "maximum_fraction": cell.maximum_symbol_concentration,
        },
        "directions": {
            "long": cell.long_count,
            "short": cell.short_count,
        },
        "closed_histogram": closed,
        "reconciliation": {
            "closed_total": closed_total,
            "planned": cell.planned,
            "scheduled": cell.scheduled,
            "closed_plus_planned_equals_scheduled": (
                closed_total + cell.planned == cell.scheduled
            ),
            "horizon_partition_exact": (
                cell.horizon_eligible + cell.fold_horizon_closed == cell.scheduled
            ),
            "winner_lifecycle_exact": (
                cell.arbitration_winners
                == cell.planned
                + cell.occupied
                + cell.entry_reference_missing
                + cell.exit_presence_missing
                + cell.order_filter_closed
            ),
            "event_terminal_exact": replay.event_terminal_exact,
        },
    }
    _assert_cell_numeric_derivation(cell, payload)
    return payload


def _walk_numeric_or_null(
    value: object,
    *,
    prefix: str = "",
) -> dict[str, int | float | None]:
    leaves: dict[str, int | float | None] = {}
    if type(value) in {int, float} or value is None:
        leaves[prefix] = value
        return leaves
    if type(value) is dict:
        for key, item in value.items():
            child = str(key) if not prefix else f"{prefix}.{key}"
            leaves.update(_walk_numeric_or_null(item, prefix=child))
    elif type(value) in {list, tuple}:
        for index, item in enumerate(value):
            child = str(index) if not prefix else f"{prefix}.{index}"
            leaves.update(_walk_numeric_or_null(item, prefix=child))
    return leaves


def _event_numeric_derivation(
    cell: CellFeasibility,
) -> dict[str, int | float | None]:
    events = cell.events
    candidates = dict.fromkeys(UNIVERSE, 0)
    winners = dict.fromkeys(UNIVERSE, 0)
    planned_by_symbol = dict.fromkeys(UNIVERSE, 0)
    closed = dict.fromkeys(CLOSED_REASON_ORDER, 0)
    movement: list[float] = []
    valid_input = 0
    dispersion_pass = 0
    common_pass = 0
    joint_pass = 0
    candidate_total = 0
    simultaneous = 0
    winner_total = 0
    long_count = 0
    short_count = 0
    for event in events:
        if event.closed_reason is not None:
            closed[event.closed_reason] += 1
        gate = event.gate
        if gate is not None:
            valid_input += type(gate.feature) is CRSFeature
            dispersion_pass += gate.dispersion_pass
            common_pass += gate.common_magnitude_pass
            joint_pass += gate.joint_pass
        arbitration = event.arbitration
        if arbitration is not None:
            candidate_total += len(arbitration.candidates)
            simultaneous += len(arbitration.candidates) == 2
            for candidate in arbitration.candidates:
                candidates[candidate.symbol] += 1
            if arbitration.winner is not None:
                winner_total += 1
                winners[arbitration.winner.symbol] += 1
                if event.planned:
                    planned_by_symbol[arbitration.winner.symbol] += 1
                    long_count += arbitration.winner.side == "LONG"
                    short_count += arbitration.winner.side == "SHORT"
        if event.movement_capacity_bp is not None:
            movement.append(event.movement_capacity_bp)

    planned = sum(event.closed_reason is None for event in events)
    horizon_eligible = sum(
        event.closed_reason != FOLD_HORIZON_CLOSED for event in events
    )
    movement_sorted = sorted(movement)
    minimum = None if not movement else min(movement)
    median = None if not movement else nearest_rank(tuple(movement), 0.50)
    mean = None if not movement else math.fsum(movement) / len(movement)
    maximum = None if not movement else max(movement)
    concentration = None if not planned else max(planned_by_symbol.values()) / planned
    answer: dict[str, int | float | None] = {
        "cell.fold_index": events[0].fold_index,
        "calendar_counts.scheduled": len(events),
        "calendar_counts.horizon_eligible": horizon_eligible,
        "calendar_counts.fold_horizon_closed": closed[FOLD_HORIZON_CLOSED],
        "valid_input": valid_input,
        "individual_gates.dispersion_pass": dispersion_pass,
        "individual_gates.common_magnitude_pass": common_pass,
        "joint_gate_pass": joint_pass,
        "symbol_candidates.directional_total": candidate_total,
        "symbol_candidates.simultaneous_cutoffs": simultaneous,
        "arbitration.winners": winner_total,
        "occupancy.closed": closed[ACCOUNT_OCCUPIED],
        "reference_availability.entry_missing": closed[ENTRY_REFERENCE_MISSING],
        "reference_availability.exit_presence_missing": closed[EXIT_PRESENCE_MISSING],
        "order_filter.closed": (
            closed[ORDER_FILTER_ZERO_QUANTITY]
            + closed[ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS]
        ),
        "planned": planned,
        "movement_capacity.count": len(movement),
        "movement_capacity.minimum": minimum,
        "movement_capacity.median": median,
        "movement_capacity.mean": mean,
        "movement_capacity.maximum": maximum,
        "symbol_concentration.maximum_fraction": concentration,
        "directions.long": long_count,
        "directions.short": short_count,
        "reconciliation.closed_total": sum(closed.values()),
        "reconciliation.planned": planned,
        "reconciliation.scheduled": len(events),
    }
    for index, value in enumerate(movement_sorted):
        answer[f"movement_capacity.values_bp.{index}"] = value
    for symbol in UNIVERSE:
        answer[f"symbol_candidates.by_symbol.{symbol}"] = candidates[symbol]
        answer[f"arbitration.by_symbol.{symbol}"] = winners[symbol]
        answer[f"symbol_concentration.planned_by_symbol.{symbol}"] = planned_by_symbol[
            symbol
        ]
    for reason in CLOSED_REASON_ORDER:
        answer[f"closed_histogram.{reason}"] = closed[reason]
    return answer


def _assert_cell_numeric_derivation(
    cell: CellFeasibility,
    payload: dict[str, object],
) -> None:
    """Cover every emitted cell numeric/null leaf with an event-ledger derivation."""
    emitted = _walk_numeric_or_null(payload)
    expected = _event_numeric_derivation(cell)
    if emitted != expected:
        missing = sorted(set(emitted) ^ set(expected))
        differing = sorted(
            key for key in set(emitted) & set(expected) if emitted[key] != expected[key]
        )
        raise ValueError(
            f"cell numeric derivation mismatch: paths={missing + differing}"
        )
    replay = cell.lifecycle_replay
    reconciliation = payload["reconciliation"]
    if type(reconciliation) is not dict:
        raise TypeError("cell reconciliation payload must be a dict")
    expected_bools = {
        "closed_plus_planned_equals_scheduled": (
            expected["reconciliation.closed_total"] + expected["reconciliation.planned"]
            == expected["reconciliation.scheduled"]
        ),
        "horizon_partition_exact": (
            expected["calendar_counts.horizon_eligible"]
            + expected["calendar_counts.fold_horizon_closed"]
            == expected["calendar_counts.scheduled"]
        ),
        "winner_lifecycle_exact": (
            expected["arbitration.winners"]
            == expected["planned"]
            + expected["occupancy.closed"]
            + expected["reference_availability.entry_missing"]
            + expected["reference_availability.exit_presence_missing"]
            + expected["order_filter.closed"]
        ),
        "event_terminal_exact": replay.event_terminal_exact,
    }
    if any(reconciliation[key] is not value for key, value in expected_bools.items()):
        raise ValueError("cell reconciliation booleans are not independently derived")


@dataclass(frozen=True, slots=True)
class CampaignTotals:
    scheduled: int
    horizon_eligible: int
    valid_input: int
    joint_gate_pass: int
    arbitration_winners: int
    occupied: int
    entry_reference_missing: int
    exit_presence_missing: int
    order_filter_closed: int
    fold_horizon_closed: int
    planned: int
    long_count: int
    short_count: int
    closed_histogram: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        for name in (
            "scheduled",
            "horizon_eligible",
            "valid_input",
            "joint_gate_pass",
            "arbitration_winners",
            "occupied",
            "entry_reference_missing",
            "exit_presence_missing",
            "order_filter_closed",
            "fold_horizon_closed",
            "planned",
            "long_count",
            "short_count",
        ):
            value = getattr(self, name)
            _exact_int(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if (
            type(self.closed_histogram) is not tuple
            or tuple(reason for reason, _count in self.closed_histogram)
            != CLOSED_REASON_ORDER
        ):
            raise ValueError("campaign closed histogram order drifted")
        for reason, count in self.closed_histogram:
            if type(reason) is not str:
                raise TypeError("closed reason must be built-in str")
            _exact_int(count, "closed count")
            if count < 0:
                raise ValueError("closed count must be non-negative")
        if self.scheduled != self.horizon_eligible + self.fold_horizon_closed:
            raise ValueError("campaign calendar reconciliation failed")
        if self.planned + sum(count for _reason, count in self.closed_histogram) != (
            self.scheduled
        ):
            raise ValueError("campaign terminal reconciliation failed")
        if self.long_count + self.short_count != self.planned:
            raise ValueError("campaign direction reconciliation failed")
        if self.arbitration_winners != (
            self.planned
            + self.occupied
            + self.entry_reference_missing
            + self.exit_presence_missing
            + self.order_filter_closed
        ):
            raise ValueError("campaign winner lifecycle reconciliation failed")


def _campaign_totals(cells: tuple[CellFeasibility, ...]) -> CampaignTotals:
    histogram = dict.fromkeys(CLOSED_REASON_ORDER, 0)
    for cell in cells:
        for item in cell.closed_histogram:
            histogram[item.reason] += item.count
    return CampaignTotals(
        scheduled=sum(cell.scheduled for cell in cells),
        horizon_eligible=sum(cell.horizon_eligible for cell in cells),
        valid_input=sum(cell.valid_input for cell in cells),
        joint_gate_pass=sum(cell.joint_gate_pass for cell in cells),
        arbitration_winners=sum(cell.arbitration_winners for cell in cells),
        occupied=sum(cell.occupied for cell in cells),
        entry_reference_missing=sum(cell.entry_reference_missing for cell in cells),
        exit_presence_missing=sum(cell.exit_presence_missing for cell in cells),
        order_filter_closed=sum(cell.order_filter_closed for cell in cells),
        fold_horizon_closed=sum(cell.fold_horizon_closed for cell in cells),
        planned=sum(cell.planned for cell in cells),
        long_count=sum(cell.long_count for cell in cells),
        short_count=sum(cell.short_count for cell in cells),
        closed_histogram=tuple(
            (reason, histogram[reason]) for reason in CLOSED_REASON_ORDER
        ),
    )


def _primary_config_id() -> str:
    primary_ids = [
        config.config_id for config in ACTIVE_CONFIGS if config.posture == "primary"
    ]
    if len(primary_ids) != 1:
        raise ValueError("expected exactly one primary CRS config")
    return primary_ids[0]


def _movement_capacity_pooled_payload(
    cells: tuple[CellFeasibility, ...],
) -> dict[str, object]:
    primary_id = _primary_config_id()
    values = sorted(
        event.movement_capacity_bp
        for cell in cells
        if cell.config_id == primary_id
        for event in cell.events
        if event.movement_capacity_bp is not None
    )
    return {
        "unit": "bp",
        "config_id": primary_id,
        "count": len(values),
        "pooled_median": None if not values else nearest_rank(tuple(values), 0.50),
        "posture": "trailing_only_diagnostic",
    }


def _totals_payload(
    cells: tuple[CellFeasibility, ...],
    totals: CampaignTotals,
) -> dict[str, object]:
    closed = dict(totals.closed_histogram)
    closed_total = sum(closed.values())
    payload = {
        "scheduled": totals.scheduled,
        "horizon_eligible": totals.horizon_eligible,
        "valid_input": totals.valid_input,
        "joint_gate_pass": totals.joint_gate_pass,
        "arbitration_winners": totals.arbitration_winners,
        "occupied": totals.occupied,
        "entry_reference_missing": totals.entry_reference_missing,
        "exit_presence_missing": totals.exit_presence_missing,
        "order_filter_closed": totals.order_filter_closed,
        "fold_horizon_closed": totals.fold_horizon_closed,
        "planned": totals.planned,
        "movement_capacity_pooled": _movement_capacity_pooled_payload(cells),
        "directions": {
            "long": totals.long_count,
            "short": totals.short_count,
        },
        "closed_histogram": closed,
        "reconciliation": {
            "closed_total": closed_total,
            "closed_plus_planned_equals_scheduled": (
                closed_total + totals.planned == totals.scheduled
            ),
            "horizon_partition_exact": (
                totals.horizon_eligible + totals.fold_horizon_closed == totals.scheduled
            ),
            "event_terminal_exact": all(
                cell.lifecycle_replay.event_terminal_exact for cell in cells
            ),
        },
    }
    _assert_campaign_numeric_derivation(cells, payload)
    return payload


def _assert_campaign_numeric_derivation(
    cells: tuple[CellFeasibility, ...],
    payload: dict[str, object],
) -> None:
    ledgers = tuple(_event_numeric_derivation(cell) for cell in cells)

    def total(path: str) -> int:
        values = tuple(ledger[path] for ledger in ledgers)
        if any(type(value) is not int for value in values):
            raise TypeError("campaign derivation expected integer ledger leaves")
        return sum(values)

    expected: dict[str, int | float | None] = {
        "scheduled": total("calendar_counts.scheduled"),
        "horizon_eligible": total("calendar_counts.horizon_eligible"),
        "valid_input": total("valid_input"),
        "joint_gate_pass": total("joint_gate_pass"),
        "arbitration_winners": total("arbitration.winners"),
        "occupied": total("occupancy.closed"),
        "entry_reference_missing": total("reference_availability.entry_missing"),
        "exit_presence_missing": total("reference_availability.exit_presence_missing"),
        "order_filter_closed": total("order_filter.closed"),
        "fold_horizon_closed": total("calendar_counts.fold_horizon_closed"),
        "planned": total("planned"),
        "directions.long": total("directions.long"),
        "directions.short": total("directions.short"),
        "reconciliation.closed_total": total("reconciliation.closed_total"),
    }
    for reason in CLOSED_REASON_ORDER:
        expected[f"closed_histogram.{reason}"] = total(f"closed_histogram.{reason}")
    primary_id = _primary_config_id()
    pooled_movement = sorted(
        event.movement_capacity_bp
        for cell in cells
        if cell.config_id == primary_id
        for event in cell.events
        if event.movement_capacity_bp is not None
    )
    expected["movement_capacity_pooled.count"] = len(pooled_movement)
    expected["movement_capacity_pooled.pooled_median"] = (
        None if not pooled_movement else nearest_rank(tuple(pooled_movement), 0.50)
    )
    emitted = _walk_numeric_or_null(payload)
    if emitted != expected:
        missing = sorted(set(emitted) ^ set(expected))
        differing = sorted(
            key for key in set(emitted) & set(expected) if emitted[key] != expected[key]
        )
        raise ValueError(
            f"campaign numeric derivation mismatch: paths={missing + differing}"
        )
    reconciliation = payload["reconciliation"]
    if type(reconciliation) is not dict:
        raise TypeError("campaign reconciliation payload must be a dict")
    expected_terminal = all(
        cell.lifecycle_replay.event_terminal_exact for cell in cells
    )
    expected_bools = {
        "closed_plus_planned_equals_scheduled": (
            expected["reconciliation.closed_total"] + expected["planned"]
            == expected["scheduled"]
        ),
        "horizon_partition_exact": (
            expected["horizon_eligible"] + expected["fold_horizon_closed"]
            == expected["scheduled"]
        ),
        "event_terminal_exact": expected_terminal,
    }
    if any(reconciliation[key] is not value for key, value in expected_bools.items()):
        raise ValueError("campaign reconciliation booleans are not derived")


class FeasibilityEvidence(Protocol):
    """Read-only view; the concrete evidence class exists only inside its issuer."""

    @property
    def cells(self) -> tuple[CellFeasibility, ...]: ...

    @property
    def totals(self) -> CampaignTotals: ...

    @property
    def evidence_sha256(self) -> str: ...

    def to_payload(self) -> dict[str, object]: ...


class ValidatedCampaignContext(Protocol):
    """Opaque test seam whose concrete instance is identity-checked by its closure."""

    @property
    def full_source_identity(self) -> str: ...

    def cells(self) -> tuple[CellFeasibility, ...]: ...

    def seal_cells(
        self,
        cells: tuple[CellFeasibility, ...],
    ) -> FeasibilityEvidence: ...


@dataclass(frozen=True, slots=True)
class CampaignInputBinding:
    """Caller-constructed ``generator``/``references`` pair, pinned once.

    Exactly two postures exist:

    * ``"frozen_synthetic_fixture"`` — the posture this module's
      zero-argument default path builds (:func:`_validated_campaign_factory`
      called with no binding). Every pin must equal this module's
      compile-time frozen ``SYNTHETIC_*`` constant. An external caller *can*
      construct such a binding, but only by supplying a generator/reference
      pair whose self-computed digests equal those frozen constants — i.e.
      only by supplying byte-identical frozen content — so this posture stays
      byte-identical to the pre-existing behavior regardless of who builds
      it. (It is separately barred from the real-corpus entry points by
      :func:`open_real_corpus_campaign_context`'s posture check.)
    * ``"refrozen_real_corpus"`` — built by an external caller (the ROB-1040
      refreeze launcher) from real 1-minute corpus data via
      :meth:`for_real_corpus`. No compile-time literal can exist for
      un-emitted real-corpus content, so every pin is instead captured once
      from the bound ``generator``/``references`` objects themselves at
      construction time and reverified against that same captured value on
      every later use — the same anti-substitution identity discipline as
      the synthetic path, without a value known in advance.

    Nothing downstream of this binding (``evaluate_cell`` and everything it
    calls) branches on posture; only the pin *source* differs.
    """

    posture: str
    version: str
    generator: CRSFeatureGenerator
    references: ReferenceSurface
    snapshot_sha256_pin: str
    entry_source_sha256_pin: str
    exit_presence_source_sha256_pin: str
    fixture_content_sha256_pin: str
    extra_authority: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.posture not in {"frozen_synthetic_fixture", "refrozen_real_corpus"}:
            raise ValueError("campaign input binding posture is unknown")
        if type(self.version) is not str or not self.version:
            raise TypeError("campaign input binding version must be a non-empty str")
        if type(self.generator) is not CRSFeatureGenerator:
            raise TypeError(
                "campaign input binding generator must be exact CRSFeatureGenerator"
            )
        if type(self.references) is not ReferenceSurface:
            raise TypeError(
                "campaign input binding references must be exact ReferenceSurface"
            )
        for name in (
            "snapshot_sha256_pin",
            "entry_source_sha256_pin",
            "exit_presence_source_sha256_pin",
            "fixture_content_sha256_pin",
        ):
            _sha256(getattr(self, name), name)
        if type(self.extra_authority) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            for item in self.extra_authority
        ):
            raise TypeError(
                "extra_authority must be an exact tuple of (str, str) pairs"
            )
        if self.posture == "frozen_synthetic_fixture":
            if (
                self.version != SYNTHETIC_FIXTURE_VERSION
                or self.snapshot_sha256_pin != SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256
                or self.entry_source_sha256_pin
                != SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256
                or self.exit_presence_source_sha256_pin
                != SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256
                or self.fixture_content_sha256_pin != SYNTHETIC_FIXTURE_CONTENT_SHA256
                or self.extra_authority != ()
            ):
                raise ValueError(
                    "synthetic posture must reuse only the frozen synthetic pins"
                )
        else:
            # Anti-substitution: a real-corpus binding must not be a relabelled
            # frozen synthetic fixture. The decisive discriminator is the JOINT
            # identity `fixture_content_sha256_pin` (generator + references
            # together) -- differing there is by itself sufficient proof that
            # this binding is not the synthetic fixture. `version`, the
            # complete-bar `snapshot_sha256_pin` and the value-bearing
            # `entry_source_sha256_pin` add independent content-bearing
            # evidence and are also required to differ.
            #
            # `exit_presence_source_sha256_pin` is DELIBERATELY EXCLUDED from
            # the must-differ set. `ReferenceSurface.exit_presence_source_sha256`
            # hashes only the {symbol, timestamp_ms} of the rows whose
            # `present` flag is True, over a FROZEN key domain (1296 keys,
            # fixed order) -- it is a presence bitmap and carries no content.
            # Consequently ANY two datasets with complete exit coverage produce
            # the identical digest by construction: the all-signal synthetic
            # calendar (1296/1296 present) and a gapless real corpus
            # (1296/1296 present) necessarily collide. Requiring that pin to
            # differ would amount to requiring the real corpus to be MISSING
            # exit bars -- the exact opposite of the desired property, and a
            # false positive on every healthy corpus. The pin is still captured
            # at construction and still reverified against the bound references
            # below and on every later use (`verify_live_inputs`), so exit
            # presence remains tamper-evident; it simply is not a posture
            # discriminator.
            if (
                self.version == SYNTHETIC_FIXTURE_VERSION
                or self.snapshot_sha256_pin == SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256
                or self.entry_source_sha256_pin
                == SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256
                or self.fixture_content_sha256_pin == SYNTHETIC_FIXTURE_CONTENT_SHA256
            ):
                raise ValueError(
                    "real-corpus posture must not reuse any frozen synthetic pin"
                )
            # Provenance is mandatory for the real posture: without it the
            # emitted evidence would carry no corpus authority at all.
            if not any(
                name == REAL_CORPUS_MANIFEST_AUTHORITY_KEY
                for name, _ in self.extra_authority
            ):
                raise ValueError(
                    "real-corpus posture requires corpus-manifest provenance authority"
                )
        if self.snapshot_sha256_pin != self.generator.snapshot_sha256:
            raise ValueError("snapshot pin does not match the bound generator")
        if self.entry_source_sha256_pin != self.references.entry_source_sha256:
            raise ValueError("entry-source pin does not match the bound references")
        if (
            self.exit_presence_source_sha256_pin
            != self.references.exit_presence_source_sha256
        ):
            raise ValueError("exit-presence pin does not match the bound references")
        if self.fixture_content_sha256_pin != fixture_content_sha256(
            self.generator, self.references
        ):
            raise ValueError(
                "full-input pin does not match the bound generator/references"
            )

    @classmethod
    def for_real_corpus(
        cls,
        *,
        version: str,
        generator: CRSFeatureGenerator,
        references: ReferenceSurface,
        corpus_manifest_content_sha256: str,
    ) -> CampaignInputBinding:
        """Build a ``refrozen_real_corpus`` binding; every pin is self-captured.

        The caller supplies only the generator/references objects and the
        corpus manifest content hash for evidence-trail purposes — every pin
        value is read back from ``generator``/``references`` themselves
        (never accepted as a bare caller-supplied string), so a mismatched or
        forged pin cannot be constructed this way.
        """
        _sha256(corpus_manifest_content_sha256, "corpus_manifest_content_sha256")
        return cls(
            posture="refrozen_real_corpus",
            version=version,
            generator=generator,
            references=references,
            snapshot_sha256_pin=generator.snapshot_sha256,
            entry_source_sha256_pin=references.entry_source_sha256,
            exit_presence_source_sha256_pin=references.exit_presence_source_sha256,
            fixture_content_sha256_pin=fixture_content_sha256(generator, references),
            extra_authority=(
                (
                    REAL_CORPUS_MANIFEST_AUTHORITY_KEY,
                    corpus_manifest_content_sha256,
                ),
            ),
        )


def _validated_campaign_factory(
    binding: CampaignInputBinding | None = None,
) -> ValidatedCampaignContext:
    """Build the sole computation context after checking every frozen input pin."""
    if binding is None:
        fixture = build_synthetic_fixture()
        generator = validate_frozen_synthetic_fixture(fixture)
        binding = CampaignInputBinding(
            posture="frozen_synthetic_fixture",
            version=fixture.version,
            generator=generator,
            references=fixture.references,
            snapshot_sha256_pin=SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256,
            entry_source_sha256_pin=SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256,
            exit_presence_source_sha256_pin=SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256,
            fixture_content_sha256_pin=SYNTHETIC_FIXTURE_CONTENT_SHA256,
            extra_authority=(),
        )
    if type(binding) is not CampaignInputBinding:
        raise TypeError("campaign input binding must be exact CampaignInputBinding")

    generator = binding.generator
    references = binding.references
    registered_configs = ACTIVE_CONFIGS
    registered_folds = exact_h4_folds()
    registered_config_snapshots = copy.deepcopy(registered_configs)
    registered_fold_snapshots = copy.deepcopy(registered_folds)
    generator_identity = id(generator)
    references_identity = id(references)

    def verify_live_inputs() -> None:
        if (
            binding.posture == "frozen_synthetic_fixture"
            and binding.version != SYNTHETIC_FIXTURE_VERSION
        ):
            raise RunAuthorityClosedError("campaign binding version changed at use")
        if registered_configs != registered_config_snapshots:
            raise RunAuthorityClosedError("campaign config state changed at use")
        if registered_folds != registered_fold_snapshots:
            raise RunAuthorityClosedError("campaign fold state changed at use")
        if type(generator) is not CRSFeatureGenerator or id(generator) != (
            generator_identity
        ):
            raise RunAuthorityClosedError("campaign generator identity changed")
        if type(references) is not ReferenceSurface or id(references) != (
            references_identity
        ):
            raise RunAuthorityClosedError("campaign reference identity changed")
        if generator.snapshot_sha256 != binding.snapshot_sha256_pin:
            raise RunAuthorityClosedError("campaign snapshot pin changed at use")
        if references.entry_source_sha256 != binding.entry_source_sha256_pin:
            raise RunAuthorityClosedError("campaign entry-source pin changed at use")
        if (
            references.exit_presence_source_sha256
            != binding.exit_presence_source_sha256_pin
        ):
            raise RunAuthorityClosedError("campaign exit-presence pin changed at use")
        if (
            fixture_content_sha256(generator, references)
            != binding.fixture_content_sha256_pin
        ):
            raise RunAuthorityClosedError("campaign full-input identity changed at use")

    verify_live_inputs()
    capability = object()
    context_instance: object | None = None
    evidence_instance: object | None = None
    issued_cells: tuple[CellFeasibility, ...] | None = None
    issued_cell_snapshots: tuple[CellFeasibility, ...] | None = None
    issued_evidence_digest: str | None = None
    issued_evidence_totals_snapshot: CampaignTotals | None = None

    def live_input_identity() -> dict[str, object]:
        verify_live_inputs()
        payload: dict[str, object] = {
            "posture": binding.posture,
            "fixture_version": binding.version,
            "complete_bar_snapshot_sha256": generator.snapshot_sha256,
            "entry_reference_source_sha256": references.entry_source_sha256,
            "exit_presence_source_sha256": references.exit_presence_source_sha256,
            "fixture_content_sha256": fixture_content_sha256(generator, references),
            "binding": "validated_campaign_context_object_identity",
        }
        if binding.extra_authority:
            payload["real_corpus_authority"] = dict(binding.extra_authority)
        return payload

    def evaluate_cell(config: object, fold: object) -> CellFeasibility:
        verify_live_inputs()
        if not any(config is registered for registered in registered_configs):
            raise RunAuthorityClosedError("unregistered config cannot enter context")
        if not any(fold is registered for registered in registered_folds):
            raise RunAuthorityClosedError("unregistered fold cannot enter context")
        cutoffs = scheduled_cutoffs(fold)
        eligible_cutoffs = tuple(
            cutoff_ms for cutoff_ms in cutoffs if is_horizon_eligible(fold, cutoff_ms)
        )
        feature_rows = generator.evaluate_cutoffs(config, eligible_cutoffs)
        gates = {row.cutoff_ms: row.evaluation for row in feature_rows.rows}
        active_exit_ts: int | None = None
        events: list[ScheduledTerminalEvent] = []
        for cutoff_ms in cutoffs:
            if not is_horizon_eligible(fold, cutoff_ms):
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        None,
                        None,
                        FOLD_HORIZON_CLOSED,
                        None,
                        None,
                        None,
                    )
                )
                continue
            gate = gates[cutoff_ms]
            if not gate.joint_pass:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        None,
                        gate.closed_reason,
                        None,
                        None,
                        None,
                    )
                )
                continue
            feature = gate.feature
            if type(feature) is not CRSFeature:
                raise ValueError("joint-pass gate lacks exact CRSFeature")
            arbitration = arbitrate(feature)
            winner = arbitration.winner
            if winner is None:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        arbitration,
                        arbitration.closed_reason,
                        None,
                        None,
                        None,
                    )
                )
                continue
            entry_ts = cutoff_ms + ENTRY_DELAY_MS
            if active_exit_ts is not None and entry_ts <= active_exit_ts:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        arbitration,
                        ACCOUNT_OCCUPIED,
                        None,
                        None,
                        None,
                    )
                )
                continue
            entry = references.entry_observation(ReferenceKey(winner.symbol, entry_ts))
            if entry.value is None:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        arbitration,
                        ENTRY_REFERENCE_MISSING,
                        entry,
                        None,
                        None,
                    )
                )
                continue
            exit_presence = references.exit_observation(
                ReferenceKey(winner.symbol, cutoff_ms + HOLD_MS + ENTRY_DELAY_MS)
            )
            if not exit_presence.present:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        arbitration,
                        EXIT_PRESENCE_MISSING,
                        entry,
                        exit_presence,
                        None,
                    )
                )
                continue
            filter_reason = order_filter_reason(winner.symbol, entry.value)
            if filter_reason is not None:
                events.append(
                    ScheduledTerminalEvent(
                        config.config_id,
                        fold.fold_id,
                        fold.fold_index,
                        cutoff_ms,
                        gate,
                        arbitration,
                        filter_reason,
                        entry,
                        exit_presence,
                        None,
                    )
                )
                continue
            events.append(
                ScheduledTerminalEvent(
                    config.config_id,
                    fold.fold_id,
                    fold.fold_index,
                    cutoff_ms,
                    gate,
                    arbitration,
                    None,
                    entry,
                    exit_presence,
                    feature.symbol(winner.symbol).movement_capacity_bp,
                )
            )
            active_exit_ts = cutoff_ms + HOLD_MS + ENTRY_DELAY_MS
        verify_live_inputs()
        return CellFeasibility(
            config.config_id,
            fold.fold_id,
            fold.fold_index,
            CONTRACT_SHA256,
            FILTER_MANIFEST_SHA256,
            FOLD_SCHEDULE_SHA256,
            feature_rows.causal_source_sha256,
            tuple(events),
        )

    def evaluate_all() -> tuple[CellFeasibility, ...]:
        nonlocal issued_cell_snapshots, issued_cells
        verify_live_inputs()
        if issued_cells is None:
            issued_cells = tuple(
                evaluate_cell(config, fold)
                for config in registered_configs
                for fold in registered_folds
            )
            issued_cell_snapshots = copy.deepcopy(issued_cells)
        verify_live_inputs()
        return issued_cells

    def require_registered_cells(cells: tuple[CellFeasibility, ...]) -> None:
        expected = evaluate_all()
        if type(cells) is not tuple or len(cells) != len(expected):
            raise RunAuthorityClosedError(
                "evidence requires the exact registered 3x8 cell tuple"
            )
        if any(
            actual is not registered
            for actual, registered in zip(cells, expected, strict=True)
        ):
            raise RunAuthorityClosedError(
                "cell was not issued by this validated context identity"
            )
        if issued_cell_snapshots is None or cells != issued_cell_snapshots:
            raise RunAuthorityClosedError(
                "registered cell state changed after context replay"
            )
        if not all(cell.lifecycle_replay.event_terminal_exact for cell in cells):
            raise RunAuthorityClosedError("cell lifecycle replay is not exact")

    def evidence_core(
        cells: tuple[CellFeasibility, ...],
        totals: CampaignTotals,
    ) -> dict[str, object]:
        """Build hashes only after object-identity registration succeeds."""
        require_registered_cells(cells)
        rendered_cells: list[dict[str, object]] = []
        for cell in cells:
            core = _cell_core_payload(cell)
            rendered_cells.append({**core, "cell_sha256": canonical_sha256(core)})
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "posture": "outcome_blind_feasibility_only",
            "cell_order": "config_major_fold_minor",
            "cell_shape": [3, 8],
            "authorities": {
                "preregistration_sha256": PREREGISTRATION_SHA256,
                "contract_sha256": CONTRACT_SHA256,
                "filter_manifest_sha256": FILTER_MANIFEST_SHA256,
                "fold_schedule_sha256": FOLD_SCHEDULE_SHA256,
                "input": live_input_identity(),
            },
            "cells": rendered_cells,
            "campaign": _totals_payload(cells, totals),
        }

    def sealed_evidence_state(
        subject: object,
    ) -> tuple[
        tuple[CellFeasibility, ...],
        CampaignTotals,
        str,
        dict[str, object],
    ]:
        """Revalidate every externally observable value from hidden issuer state."""
        if subject is not evidence_instance:
            raise RunAuthorityClosedError("unregistered evidence identity")
        cells = evaluate_all()
        require_registered_cells(cells)
        totals = _campaign_totals(cells)
        if (
            issued_evidence_totals_snapshot is None
            or totals != issued_evidence_totals_snapshot
        ):
            raise RunAuthorityClosedError("evidence totals state changed")
        core = evidence_core(cells, totals)
        digest = canonical_sha256(core)
        if issued_evidence_digest is None or digest != issued_evidence_digest:
            raise RunAuthorityClosedError("evidence payload identity changed")
        if (
            binding.posture == "frozen_synthetic_fixture"
            and digest != FROZEN_SYNTHETIC_EVIDENCE_SHA256
        ):
            raise RunAuthorityClosedError("frozen synthetic evidence pin changed")
        return cells, totals, digest, core

    class _Evidence:
        """State-free capability handle; all seal material remains in the closure."""

        __slots__ = ()

        def __init__(self, presented: object) -> None:
            if presented is not capability:
                raise RunAuthorityClosedError("evidence constructor capability denied")

        @property
        def cells(self) -> tuple[CellFeasibility, ...]:
            cells, _totals, _digest, _core = sealed_evidence_state(self)
            return cells

        @property
        def totals(self) -> CampaignTotals:
            _cells, totals, _digest, _core = sealed_evidence_state(self)
            return totals

        @property
        def evidence_sha256(self) -> str:
            _cells, _totals, digest, _core = sealed_evidence_state(self)
            return digest

        def to_payload(self) -> dict[str, object]:
            _cells, _totals, digest, core = sealed_evidence_state(self)
            return {**core, "evidence_sha256": digest}

    def issue_evidence(cells: tuple[CellFeasibility, ...]) -> FeasibilityEvidence:
        nonlocal evidence_instance
        nonlocal issued_evidence_digest
        nonlocal issued_evidence_totals_snapshot
        require_registered_cells(cells)
        if evidence_instance is None:
            totals = _campaign_totals(cells)
            digest = canonical_sha256(evidence_core(cells, totals))
            _sha256(digest, "evidence_sha256")
            issued_evidence_totals_snapshot = copy.deepcopy(totals)
            issued_evidence_digest = digest
            evidence_instance = _Evidence(capability)
        if type(evidence_instance) is not _Evidence:
            raise RunAuthorityClosedError("evidence identity registry changed")
        sealed_evidence_state(evidence_instance)
        return evidence_instance

    class _Context:
        __slots__ = ()

        def __init__(self, presented: object) -> None:
            if presented is not capability:
                raise RunAuthorityClosedError("context constructor capability denied")

        def _require_identity(self) -> None:
            if self is not context_instance:
                raise RunAuthorityClosedError("unregistered campaign context identity")
            verify_live_inputs()

        @property
        def full_source_identity(self) -> str:
            self._require_identity()
            return live_input_identity()["fixture_content_sha256"]

        def cells(self) -> tuple[CellFeasibility, ...]:
            self._require_identity()
            return evaluate_all()

        def seal_cells(
            self,
            cells: tuple[CellFeasibility, ...],
        ) -> FeasibilityEvidence:
            self._require_identity()
            return issue_evidence(cells)

    context_instance = _Context(capability)
    return context_instance


def open_frozen_synthetic_test_seam() -> ValidatedCampaignContext:
    """Return a newly validated, opaque synthetic context with no input arguments."""
    return _validated_campaign_factory()


def run_frozen_synthetic_cells() -> tuple[CellFeasibility, ...]:
    """Compute only the internally created and fully pinned synthetic campaign."""
    context = _validated_campaign_factory()
    return context.cells()


def build_frozen_synthetic_evidence() -> FeasibilityEvidence:
    """Issue evidence only for cells registered by one validated context identity."""
    context = _validated_campaign_factory()
    cells = context.cells()
    return context.seal_cells(cells)


def open_real_corpus_campaign_context(
    binding: CampaignInputBinding,
) -> ValidatedCampaignContext:
    """Open a validated context from a caller-supplied real-corpus binding.

    Only an exact ``posture="refrozen_real_corpus"`` binding is accepted —
    a synthetic-posture binding can only ever be constructed by this
    module's own zero-argument default path
    (:func:`open_frozen_synthetic_test_seam`/
    :func:`build_frozen_synthetic_evidence`), never by a caller, so every
    synthetic invocation stays on the byte-identical frozen path and no
    caller can claim the synthetic posture for substituted content.
    """
    if type(binding) is not CampaignInputBinding or binding.posture != (
        "refrozen_real_corpus"
    ):
        raise RunAuthorityClosedError(
            "real-corpus campaign context requires an exact refrozen_real_corpus"
            " binding"
        )
    return _validated_campaign_factory(binding)


def build_real_corpus_evidence(
    binding: CampaignInputBinding,
) -> FeasibilityEvidence:
    """Issue evidence for cells computed from a caller-supplied real-corpus
    binding. The decision logic invoked is identical to the synthetic path
    (same ``evaluate_cell``/``evaluate_all``/``evidence_core`` closures);
    only the bound ``generator``/``references`` differ."""
    context = open_real_corpus_campaign_context(binding)
    cells = context.cells()
    return context.seal_cells(cells)


def build_evidence(*_args: object, **_kwargs: object) -> None:
    raise RunAuthorityClosedError(
        "caller-supplied evidence issuance is closed pending merge/refreeze/approval"
    )


def cell_payload(*_args: object, **_kwargs: object) -> None:
    raise RunAuthorityClosedError(
        "caller-supplied cell sealing is closed pending merge/refreeze/approval"
    )


def render_evidence_bytes() -> bytes:
    """Render only a freshly validated, internally issued synthetic evidence."""
    payload = build_frozen_synthetic_evidence().to_payload()
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "CampaignInputBinding",
    "CampaignTotals",
    "EVIDENCE_SCHEMA_VERSION",
    "FROZEN_SYNTHETIC_EVIDENCE_SHA256",
    "FeasibilityEvidence",
    "ValidatedCampaignContext",
    "build_evidence",
    "build_frozen_synthetic_evidence",
    "build_real_corpus_evidence",
    "cell_payload",
    "open_frozen_synthetic_test_seam",
    "open_real_corpus_campaign_context",
    "render_evidence_bytes",
    "run_frozen_synthetic_cells",
]
