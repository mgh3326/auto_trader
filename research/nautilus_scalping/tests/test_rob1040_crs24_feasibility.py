from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
import rob1040_crs24_feasibility as feasibility
from rob974_h4_contracts import exact_h4_folds
from rob1040_crs24_contracts import (
    ACTIVE_CONFIGS,
    ALL_SIGNAL_OCCUPIED_PER_FOLD,
    ALL_SIGNAL_PLANNED_PER_FOLD,
    CONTRACT_SHA256,
    FILTER_MANIFEST_SHA256,
    FOLD_HORIZON_CLOSED_PER_FOLD,
    FOLD_SCHEDULE_SHA256,
    HORIZON_ELIGIBLE_PER_FOLD,
    SCHEDULED_PER_FOLD,
)
from rob1040_crs24_evidence import run_frozen_synthetic_cells
from rob1040_crs24_feasibility import (
    ACCOUNT_OCCUPIED,
    DISPERSION_GATE_CLOSED,
    ENTRY_REFERENCE_MISSING,
    FOLD_HORIZON_CLOSED,
    ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS,
    ORDER_FILTER_ZERO_QUANTITY,
    CellFeasibility,
    EntryReference,
    ExitPresence,
    ReferenceKey,
    ReferenceSurface,
    RunAuthorityClosedError,
    ScheduledTerminalEvent,
    is_horizon_eligible,
    order_filter_reason,
    run_all_cells,
    run_cell,
    scheduled_cutoffs,
    synthetic_all_signal_occupancy,
)
from rob1040_crs24_features import (
    INPUT_HISTORY_INCOMPLETE,
    CRSFeatureGenerator,
    FeatureClosed,
    PITGateEvaluation,
)
from rob1040_crs24_synthetic import (
    build_synthetic_fixture,
    validate_frozen_synthetic_fixture,
)


@pytest.fixture(scope="module")
def synthetic_bundle():
    fixture = build_synthetic_fixture()
    bars = fixture.bars_by_symbol()
    return fixture, bars, CRSFeatureGenerator(bars)


@pytest.fixture(scope="module")
def synthetic_cells():
    return run_frozen_synthetic_cells()


def test_calendar_authority_is_56_scheduled_54_eligible_2_closed() -> None:
    for fold in exact_h4_folds():
        cutoffs = scheduled_cutoffs(fold)
        eligible = tuple(
            cutoff for cutoff in cutoffs if is_horizon_eligible(fold, cutoff)
        )
        assert len(cutoffs) == SCHEDULED_PER_FOLD == 56
        assert len(eligible) == HORIZON_ELIGIBLE_PER_FOLD == 54
        assert len(cutoffs) - len(eligible) == FOLD_HORIZON_CLOSED_PER_FOLD == 2
        assert all(cutoff % (12 * 60 * 60 * 1000) == 0 for cutoff in cutoffs)
        assert (
            tuple(cutoff for cutoff in cutoffs if cutoff not in eligible)
            == cutoffs[-2:]
        )


def test_all_signal_occupancy_includes_equal_exit_and_next_entry() -> None:
    for fold in exact_h4_folds():
        planned, occupied = synthetic_all_signal_occupancy(fold)
        assert planned == ALL_SIGNAL_PLANNED_PER_FOLD == 18
        assert occupied == ALL_SIGNAL_OCCUPIED_PER_FOLD == 36
    fold = exact_h4_folds()[0]
    eligible = tuple(
        cutoff
        for cutoff in scheduled_cutoffs(fold)
        if is_horizon_eligible(fold, cutoff)
    )
    first_exit = eligible[0] + 24 * 60 * 60 * 1000 + 60_000
    third_entry = eligible[2] + 60_000
    assert first_exit == third_entry


def test_static_filter_floors_to_step_and_enforces_inclusive_range() -> None:
    assert order_filter_reason("XRPUSDT", Decimal("0.6")) is None
    assert order_filter_reason("DOGEUSDT", Decimal("0.2")) is None
    assert order_filter_reason("SOLUSDT", Decimal("150")) is None
    assert (
        order_filter_reason("SOLUSDT", Decimal("550"))
        == ORDER_FILTER_NOTIONAL_OUTSIDE_BOUNDS
    )
    assert order_filter_reason("XRPUSDT", Decimal("1000")) == ORDER_FILTER_ZERO_QUANTITY


def test_full_synthetic_cell_is_derived_from_exact_terminal_events(
    synthetic_cells,
) -> None:
    cell = synthetic_cells[0]
    histogram = {item.reason: item.count for item in cell.closed_histogram}
    assert cell.scheduled == len(cell.events) == 56
    assert len({event.cutoff_ms for event in cell.events}) == 56
    assert cell.horizon_eligible == 54
    assert cell.fold_horizon_closed == histogram[FOLD_HORIZON_CLOSED] == 2
    assert cell.valid_input == 54
    assert cell.arbitration_winners == cell.planned + cell.occupied
    assert cell.occupied == histogram[ACCOUNT_OCCUPIED]
    assert sum(histogram.values()) + cell.planned == cell.scheduled
    assert cell.movement_capacity.count == cell.planned
    assert cell.long_count + cell.short_count == cell.planned
    assert sum(item.count for item in cell.planned_by_symbol) == cell.planned
    assert cell.lifecycle_replay.event_terminal_exact


def test_undefined_symbol_concentration_is_json_null_semantics() -> None:
    config = ACTIVE_CONFIGS[0]
    fold = exact_h4_folds()[0]
    events: list[ScheduledTerminalEvent] = []
    for cutoff_ms in scheduled_cutoffs(fold):
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
        closed = FeatureClosed(
            config.config_id,
            cutoff_ms,
            INPUT_HISTORY_INCOMPLETE,
        )
        gate = PITGateEvaluation(
            closed,
            0,
            None,
            None,
            False,
            False,
            False,
            INPUT_HISTORY_INCOMPLETE,
        )
        events.append(
            ScheduledTerminalEvent(
                config.config_id,
                fold.fold_id,
                fold.fold_index,
                cutoff_ms,
                gate,
                None,
                INPUT_HISTORY_INCOMPLETE,
                None,
                None,
                None,
            )
        )
    cell = CellFeasibility(
        config.config_id,
        fold.fold_id,
        fold.fold_index,
        CONTRACT_SHA256,
        FILTER_MANIFEST_SHA256,
        FOLD_SCHEDULE_SHA256,
        "1" * 64,
        tuple(events),
    )
    assert cell.planned == 0
    assert cell.maximum_symbol_concentration is None
    assert cell.movement_capacity.minimum_bp is None


def test_reference_surface_has_explicit_missing_sentinels_and_exact_domain(
    synthetic_bundle,
) -> None:
    fixture, _bars, _generator = synthetic_bundle
    entry_missing = ReferenceSurface(
        tuple(
            dataclasses.replace(item, value=None) for item in fixture.references.entries
        ),
        fixture.references.exit_presence,
    )
    exit_missing = ReferenceSurface(
        fixture.references.entries,
        tuple(
            dataclasses.replace(item, present=False)
            for item in fixture.references.exit_presence
        ),
    )
    assert all(item.value is None for item in entry_missing.entries)
    assert not any(item.present for item in exit_missing.exit_presence)
    assert entry_missing.entry_source_sha256 != fixture.references.entry_source_sha256
    assert (
        exit_missing.exit_presence_source_sha256
        != fixture.references.exit_presence_source_sha256
    )


def test_reference_and_filter_failure_cannot_create_lifecycle_state(
    synthetic_cells,
) -> None:
    cell = next(
        candidate
        for candidate in synthetic_cells
        if any(event.closed_reason == ACCOUNT_OCCUPIED for event in candidate.events)
    )
    occupied_index = next(
        index
        for index, event in enumerate(cell.events)
        if event.closed_reason == ACCOUNT_OCCUPIED
    )
    planned_index = max(
        index
        for index, event in enumerate(cell.events[:occupied_index])
        if event.planned
    )
    planned = cell.events[planned_index]
    missing_entry = dataclasses.replace(
        planned.entry_observation,
        value=None,
    )
    failed = dataclasses.replace(
        planned,
        closed_reason=ENTRY_REFERENCE_MISSING,
        entry_observation=missing_entry,
        exit_observation=None,
        movement_capacity_bp=None,
    )
    mutated = (*cell.events[:planned_index], failed, *cell.events[planned_index + 1 :])
    with pytest.raises(ValueError, match="without an active position"):
        dataclasses.replace(cell, events=mutated)

    blocked_entry = dataclasses.replace(
        planned.entry_observation,
        value=Decimal("1000"),
    )
    filter_failed = dataclasses.replace(
        planned,
        closed_reason=ORDER_FILTER_ZERO_QUANTITY,
        entry_observation=blocked_entry,
        movement_capacity_bp=None,
    )
    mutated = (
        *cell.events[:planned_index],
        filter_failed,
        *cell.events[planned_index + 1 :],
    )
    with pytest.raises(ValueError, match="without an active position"):
        dataclasses.replace(cell, events=mutated)


def test_first_planned_event_cannot_be_relabelled_occupied(
    synthetic_cells,
) -> None:
    cell = next(candidate for candidate in synthetic_cells if candidate.planned)
    planned_index = next(
        index for index, event in enumerate(cell.events) if event.planned
    )
    planned = cell.events[planned_index]
    forged = dataclasses.replace(
        planned,
        closed_reason=ACCOUNT_OCCUPIED,
        entry_observation=None,
        exit_observation=None,
        movement_capacity_bp=None,
    )
    mutated = (*cell.events[:planned_index], forged, *cell.events[planned_index + 1 :])
    with pytest.raises(ValueError, match="without an active position"):
        dataclasses.replace(cell, events=mutated)


def test_reference_surface_rejects_extra_and_missing_keys() -> None:
    fixture = build_synthetic_fixture()
    with pytest.raises(ValueError, match="exact frozen key domain"):
        ReferenceSurface(
            fixture.references.entries[:-1],
            fixture.references.exit_presence,
        )
    extra = EntryReference(ReferenceKey("XRPUSDT", 60_000), Decimal("0.5"))
    with pytest.raises(ValueError, match="exact frozen key domain"):
        ReferenceSurface(
            (*fixture.references.entries, extra),
            fixture.references.exit_presence,
        )
    with pytest.raises(ValueError, match="exact frozen key domain"):
        ReferenceSurface(
            fixture.references.entries,
            fixture.references.exit_presence[:-1],
        )
    assert tuple(field.name for field in dataclasses.fields(ExitPresence)) == (
        "key",
        "present",
    )
    with pytest.raises(TypeError, match="bool"):
        ExitPresence(fixture.references.exit_presence[0].key, Decimal("1"))  # type: ignore[arg-type]


def test_frozen_fixture_validator_rejects_any_caller_content_change(
    synthetic_bundle,
) -> None:
    fixture, bars, _generator = synthetic_bundle
    changed = dict(bars)
    first = changed["DOGEUSDT"][0]
    changed["DOGEUSDT"] = (
        dataclasses.replace(first, volume=first.volume + 1.0),
        *changed["DOGEUSDT"][1:],
    )
    changed_fixture = dataclasses.replace(
        fixture,
        series=tuple(
            dataclasses.replace(item, bars=changed[item.symbol])
            for item in fixture.series
        ),
    )
    with pytest.raises(ValueError, match="content identity"):
        validate_frozen_synthetic_fixture(changed_fixture)


def test_terminal_reason_relabeling_is_rejected_at_the_row(
    synthetic_cells,
) -> None:
    cell = synthetic_cells[0]
    event = next(
        item for item in cell.events if item.closed_reason == DISPERSION_GATE_CLOSED
    )
    with pytest.raises(ValueError, match="not truthful"):
        dataclasses.replace(event, closed_reason=ENTRY_REFERENCE_MISSING)
    with pytest.raises(TypeError):
        dataclasses.replace(cell, closed_histogram=cell.closed_histogram)


def test_no_public_authority_dto_or_evaluator_and_sentinels_are_closed() -> None:
    assert not hasattr(feasibility, "InputAuthority")
    assert not hasattr(feasibility, "_evaluate_cell")
    assert not hasattr(feasibility, "_evaluate_all_cells")
    with pytest.raises(RunAuthorityClosedError, match="closed pending"):
        run_cell(object())
    with pytest.raises(RunAuthorityClosedError, match="closed pending"):
        run_all_cells(object())
