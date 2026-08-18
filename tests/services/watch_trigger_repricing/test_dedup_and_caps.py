"""ROB-1286 §4/AC4 — dedup, per-symbol concurrency, per-round cap.

Each test is written so that removing the guard it covers makes it fail.
The mutant runs proving that are recorded in the ROB-1286 report.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.spawn import DrySessionSpawner

from .conftest import INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit


def _spawned_uuids(result) -> list[str]:
    return [o.request.event_uuid for o in result.spawned]


# ---------------------------------------------------------------------------
# 1. event-uuid dedup: one fire spawns once, across ticks
# ---------------------------------------------------------------------------
def test_same_event_is_not_spawned_twice_across_ticks(
    store: InMemoryClaimStore,
) -> None:
    event = make_event(event_uuid="evt-1")

    first = run_repricing_tick([event], store=store, now=INCIDENT_TICK)
    second = run_repricing_tick(
        [event], store=store, now=INCIDENT_TICK + dt.timedelta(minutes=1)
    )

    assert _spawned_uuids(first) == ["evt-1"]
    assert _spawned_uuids(second) == []
    assert [s.reason for s in second.skipped] == ["already_consumed"]


def test_same_event_twice_in_one_tick_spawns_once(
    store: InMemoryClaimStore,
) -> None:
    """A duplicated poll row must not become two sessions."""
    event = make_event(event_uuid="evt-1")

    result = run_repricing_tick([event, event], store=store, now=INCIDENT_TICK)

    assert _spawned_uuids(result) == ["evt-1"]


# ---------------------------------------------------------------------------
# 2. per-symbol concurrency: at most one in-flight session per symbol
# ---------------------------------------------------------------------------
def test_two_events_on_one_symbol_spawn_only_one(
    store: InMemoryClaimStore,
) -> None:
    """The real 08-18 shape: ladder rung 1 and rung 2 fire on 005930."""
    rung1 = make_event(event_uuid="evt-rung1", symbol="005930")
    rung2 = make_event(event_uuid="evt-rung2", symbol="005930")

    result = run_repricing_tick([rung1, rung2], store=store, now=INCIDENT_TICK)

    assert len(result.spawned) == 1
    assert [s.reason for s in result.skipped] == ["symbol_already_in_flight"]


def test_symbol_concurrency_holds_across_ticks(store: InMemoryClaimStore) -> None:
    rung1 = make_event(event_uuid="evt-rung1", symbol="005930")
    rung2 = make_event(event_uuid="evt-rung2", symbol="005930")

    run_repricing_tick([rung1], store=store, now=INCIDENT_TICK)
    second = run_repricing_tick(
        [rung2], store=store, now=INCIDENT_TICK + dt.timedelta(minutes=1)
    )

    assert second.spawned == ()
    assert [s.reason for s in second.skipped] == ["symbol_already_in_flight"]


def test_different_symbols_are_not_blocked_by_each_other(
    store: InMemoryClaimStore,
) -> None:
    """The concurrency guard must be per symbol, not a global lock."""
    result = run_repricing_tick(
        [
            make_event(event_uuid="evt-a", symbol="005930"),
            make_event(event_uuid="evt-b", symbol="039200"),
        ],
        store=store,
        now=INCIDENT_TICK,
    )

    assert len(result.spawned) == 2


# ---------------------------------------------------------------------------
# 3. per-round cap + overflow is surfaced, never silently dropped
# ---------------------------------------------------------------------------
def test_round_cap_bounds_spawns_per_tick(store: InMemoryClaimStore) -> None:
    events = [make_event(event_uuid=f"evt-{i}", symbol=f"sym-{i}") for i in range(5)]

    result = run_repricing_tick(events, store=store, now=INCIDENT_TICK, round_cap=3)

    assert len(result.spawned) == 3
    assert result.overflow_count == 2


def test_overflow_is_reported_with_symbol_and_event_uuid(
    store: InMemoryClaimStore,
) -> None:
    """§4: 'silently dropped' would be the original accident, re-made."""
    events = [make_event(event_uuid=f"evt-{i}", symbol=f"sym-{i}") for i in range(5)]

    result = run_repricing_tick(events, store=store, now=INCIDENT_TICK, round_cap=3)

    assert {(s.event_uuid, s.symbol) for s in result.overflow} == {
        ("evt-3", "sym-3"),
        ("evt-4", "sym-4"),
    }
    assert {s.reason for s in result.overflow} == {"round_cap_exceeded"}


def test_overflow_is_logged_at_warning(
    store: InMemoryClaimStore, caplog: pytest.LogCaptureFixture
) -> None:
    events = [make_event(event_uuid=f"evt-{i}", symbol=f"sym-{i}") for i in range(4)]

    with caplog.at_level("WARNING", logger="app.services.watch_trigger_repricing"):
        run_repricing_tick(events, store=store, now=INCIDENT_TICK, round_cap=3)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("evt-3" in r.getMessage() for r in warnings)
    assert any("round cap" in r.getMessage() for r in warnings)


def test_capped_event_is_still_spawnable_on_a_later_tick(
    store: InMemoryClaimStore,
) -> None:
    """Overflow defers a fire; it must not consume or bury it."""
    events = [make_event(event_uuid=f"evt-{i}", symbol=f"sym-{i}") for i in range(4)]

    first = run_repricing_tick(events, store=store, now=INCIDENT_TICK, round_cap=3)
    deferred = first.overflow[0].event_uuid

    second = run_repricing_tick(
        [e for e in events if e.event_uuid == deferred],
        store=store,
        now=INCIDENT_TICK + dt.timedelta(minutes=1),
        round_cap=3,
    )

    assert _spawned_uuids(second) == [deferred]


# ---------------------------------------------------------------------------
# 4. the spawner really is dry
# ---------------------------------------------------------------------------
def test_default_spawner_starts_nothing(store: InMemoryClaimStore) -> None:
    spawner = DrySessionSpawner()

    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=spawner,
    )

    assert len(spawner.requests) == 1
    assert all(o.started is False for o in result.spawned)
    assert all(o.detail == "dry_run" for o in result.spawned)
