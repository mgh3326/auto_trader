"""ROB-1286 r2 / BLOCKER-1 — dedup that survives the flow-run boundary.

r1's dedup tests all shared one ``InMemoryClaimStore`` handed in by a
fixture. The real entrypoint built a fresh store per call, so "the same
event is not spawned twice across ticks" was true of the fixture and false
of the deployment. And even within one store, a *successful* spawn became
reclaimable when its 30-minute lease lapsed, so the fire was re-judged
half an hour later.

These tests drive ``run_gated_tick`` -- the actual entrypoint, with no
store injected -- and assert on the two things r1 got wrong:

* one fire, two separate flow runs, one spawn;
* a lease expiry that revives a *crashed* claim but never a *successful*
  one.

What is still not closed, and cannot be here: these runs share a process.
Prefect flow runs do not. Durable cross-process dedup needs a claim table
with a UNIQUE constraint on ``event_uuid``, which is a migration and is
approval-gated. ``test_live_spawner_is_refused_against_a_volatile_store``
below is the guard that stops that gap from being armed by accident.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    InMemoryClaimStore,
)
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.orchestrator import (
    process_claim_store,
    run_gated_tick,
    run_repricing_tick,
)
from app.services.watch_trigger_repricing.spawn import (
    DrySessionSpawner,
    SpawnDisposition,
    SpawnNotStarted,
    SpawnOutcome,
)

from .conftest import INCIDENT_TICK, make_event

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("enabled")]


class LiveLookingSpawner:
    """Reports a real, started session. Starts nothing -- it is a stand-in.

    ``is_dry`` is False so it exercises the arming gate; ``spawn`` only
    appends to a list. No session, no proposal, no broker.
    """

    def __init__(self) -> None:
        self.requests: list[object] = []

    @property
    def is_dry(self) -> bool:
        return False

    def spawn(self, request):
        self.requests.append(request)
        return SpawnOutcome(
            request=request,
            disposition=SpawnDisposition.STARTED,
            detail="stand-in: recorded, not started",
        )


class DurableLookingStore(InMemoryClaimStore):
    """In-memory store that *claims* durability, for gate tests only."""

    @property
    def is_durable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 1. The run boundary
# ---------------------------------------------------------------------------
def test_two_flow_runs_share_one_claim_store() -> None:
    """The entrypoint must not build a fresh store per run (r1's bug)."""
    run_gated_tick(events=[], now=INCIDENT_TICK)
    first = process_claim_store()
    run_gated_tick(events=[], now=INCIDENT_TICK)
    second = process_claim_store()

    assert first is second


def test_same_event_is_not_spawned_twice_across_flow_runs() -> None:
    """One fire, two independent ``run_gated_tick`` calls, one spawn.

    No store argument: this is the code path the Prefect wrapper uses.
    """
    event = make_event(event_uuid="evt-crossrun")

    first = run_gated_tick(events=[event], now=INCIDENT_TICK)
    second = run_gated_tick(events=[event], now=INCIDENT_TICK + dt.timedelta(minutes=1))

    assert [s["eventUuid"] for s in first["spawned"]] == ["evt-crossrun"]
    assert second["spawned"] == []
    assert [s["reason"] for s in second["skipped"]] == ["already_consumed"]


def test_dedup_across_flow_runs_holds_past_the_lease() -> None:
    """The two failures compose: different run *and* expired lease."""
    event = make_event(event_uuid="evt-crossrun-late")

    run_gated_tick(events=[event], now=INCIDENT_TICK)
    later = run_gated_tick(
        events=[event], now=INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=5)
    )

    assert later["spawned"] == []
    assert [s["reason"] for s in later["skipped"]] == ["already_consumed"]


# ---------------------------------------------------------------------------
# 2. Success and failure must expire differently
# ---------------------------------------------------------------------------
def test_successful_claim_is_never_revived_by_lease_expiry(
    store: InMemoryClaimStore,
) -> None:
    """The headline BLOCKER-1 regression: 30 minutes later, no re-spawn."""
    event = make_event(event_uuid="evt-1")
    run_repricing_tick([event], store=store, now=INCIDENT_TICK)

    long_after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(hours=2)
    assert store.state_for("evt-1", now=long_after) is ConsumptionState.CONSUMED

    revived = run_repricing_tick([event], store=store, now=long_after)
    assert revived.spawned == ()
    assert [s.reason for s in revived.skipped] == ["already_consumed"]


def test_a_crashed_claim_still_self_heals_after_the_lease(
    store: InMemoryClaimStore,
) -> None:
    """The other direction must be preserved: a dead tick must not bury it.

    This is the §0 behaviour the terminal state must not break -- only a
    *finalised* claim is permanent; a bare lease still expires.
    """
    store.try_claim(
        event_uuid="evt-1",
        symbol="005930",
        claimed_by="tick-that-died",
        now=INCIDENT_TICK,
    )
    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)

    assert store.state_for("evt-1", now=after) is ConsumptionState.UNCLAIMED
    revived = run_repricing_tick(
        [make_event(event_uuid="evt-1")], store=store, now=after
    )
    assert [o.request.event_uuid for o in revived.spawned] == ["evt-1"]


def test_a_proven_failed_spawn_is_retried_on_the_next_tick(
    store: InMemoryClaimStore,
) -> None:
    """Success is permanent; a proven failure is not. Both, in one store."""

    class FailsOnceSpawner:
        is_dry = True

        def __init__(self) -> None:
            self.calls = 0

        def spawn(self, request):
            self.calls += 1
            if self.calls == 1:
                raise SpawnNotStarted("backend refused")
            return SpawnOutcome(
                request=request,
                disposition=SpawnDisposition.DRY,
                detail="dry_run",
            )

    spawner = FailsOnceSpawner()
    event = make_event(event_uuid="evt-1")

    first = run_repricing_tick([event], store=store, now=INCIDENT_TICK, spawner=spawner)
    second = run_repricing_tick(
        [event],
        store=store,
        now=INCIDENT_TICK + dt.timedelta(minutes=1),
        spawner=spawner,
    )

    assert first.spawned == ()
    assert [s.reason for s in first.skipped] == ["spawn_not_started"]
    assert [o.request.event_uuid for o in second.spawned] == ["evt-1"]


def test_lease_expiry_frees_the_symbol_but_not_the_event(
    store: InMemoryClaimStore,
) -> None:
    """Two clocks. A consumed fire is done forever; its symbol is not.

    Without this split, consuming one 005930 fire at 09:06 would mute every
    later 005930 fire for the rest of the session.
    """
    first_fire = make_event(event_uuid="evt-rung1", symbol="005930")
    later_fire = make_event(event_uuid="evt-rung2", symbol="005930")

    run_repricing_tick([first_fire], store=store, now=INCIDENT_TICK)
    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)

    # Symbol is free again...
    assert "005930" not in store.active_symbols(now=after)
    result = run_repricing_tick([later_fire], store=store, now=after)
    assert [o.request.event_uuid for o in result.spawned] == ["evt-rung2"]

    # ...but the original event is still terminally consumed.
    assert store.state_for("evt-rung1", now=after) is ConsumptionState.CONSUMED


def test_try_claim_itself_refuses_a_consumed_event_after_the_lease() -> None:
    """The store primitive must refuse it, not just the selection above it.

    ``select_candidates`` also checks ``state_for`` and would skip the
    event, so a test that only drives the orchestrator passes even if
    ``try_claim`` happily reclaims terminal records -- the outer check
    masks the inner bug. This asserts the inner one directly, so the
    permanence lives in the primitive that a durable store will have to
    reimplement.
    """
    store = InMemoryClaimStore()
    store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="a", now=INCIDENT_TICK
    )
    store.mark_consumed("evt-1", reason="spawn_started")
    long_after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(hours=4)

    # The lease is long gone...
    assert "005930" not in store.active_symbols(now=long_after)
    # ...and the event is still not takeable, by anyone.
    assert (
        store.try_claim(
            event_uuid="evt-1", symbol="005930", claimed_by="b", now=long_after
        )
        is None
    )
    assert store.state_for("evt-1", now=long_after) is ConsumptionState.CONSUMED


def test_try_claim_itself_refuses_a_quarantined_event_after_the_lease() -> None:
    """Same permanence for the ambiguous-spawn terminal state."""
    store = InMemoryClaimStore()
    store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="a", now=INCIDENT_TICK
    )
    store.quarantine("evt-1", reason="ambiguous spawn")
    long_after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(hours=4)

    assert (
        store.try_claim(
            event_uuid="evt-1", symbol="005930", claimed_by="b", now=long_after
        )
        is None
    )
    assert store.state_for("evt-1", now=long_after) is ConsumptionState.QUARANTINED


def test_a_bare_lease_is_still_reclaimable_after_expiry() -> None:
    """The contrast case: only *finalised* claims are permanent."""
    store = InMemoryClaimStore()
    store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="a", now=INCIDENT_TICK
    )
    long_after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)

    assert (
        store.try_claim(
            event_uuid="evt-1", symbol="005930", claimed_by="b", now=long_after
        )
        is not None
    )


def test_a_terminal_claim_cannot_be_released(store: InMemoryClaimStore) -> None:
    """Releasing a consumed claim would re-open a handled fire."""
    from app.services.watch_trigger_repricing.claims import TerminalClaimNotReleasable

    run_repricing_tick([make_event(event_uuid="evt-1")], store=store, now=INCIDENT_TICK)

    with pytest.raises(TerminalClaimNotReleasable):
        store.release("evt-1", reason="oops")


# ---------------------------------------------------------------------------
# 3. The arming gate that stands in for the migration
# ---------------------------------------------------------------------------
def test_live_spawner_is_refused_against_a_volatile_store() -> None:
    """A non-dry spawner + a process-local store is refused, in code.

    This is what makes "safe when attached" true rather than "safe because
    nothing is attached yet": the in-memory store cannot be used to arm a
    live spawner even if someone wires one up.
    """
    spawner = LiveLookingSpawner()

    out = run_gated_tick(
        events=[make_event(event_uuid="evt-1")],
        spawner=spawner,
        now=INCIDENT_TICK,
    )

    assert out["status"] == "blocked"
    assert out["reason"] == "non_durable_claim_store"
    assert out["spawned"] == []
    assert spawner.requests == []


def test_live_spawner_runs_only_against_a_durable_store() -> None:
    """The gate is about durability, not about refusing live spawners."""
    spawner = LiveLookingSpawner()

    out = run_gated_tick(
        events=[make_event(event_uuid="evt-1")],
        store=DurableLookingStore(),
        spawner=spawner,
        now=INCIDENT_TICK,
    )

    assert out["status"] == "ok"
    assert [s["eventUuid"] for s in out["spawned"]] == ["evt-1"]


def test_a_spawner_that_does_not_declare_dryness_is_treated_as_live() -> None:
    """Fail-closed: silence means live, not safe."""

    class UndeclaredSpawner:
        def spawn(self, request):  # pragma: no cover - must never be reached
            raise AssertionError("spawn must not be called")

    out = run_gated_tick(
        events=[make_event(event_uuid="evt-1")],
        spawner=UndeclaredSpawner(),
        now=INCIDENT_TICK,
    )

    assert out["status"] == "blocked"
    assert out["reason"] == "non_durable_claim_store"


def test_the_shipped_store_reports_itself_volatile() -> None:
    assert InMemoryClaimStore().is_durable is False
    assert DrySessionSpawner().is_dry is True
