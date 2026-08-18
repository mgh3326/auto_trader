"""ROB-1286 r2 / BLOCKER-2 — real concurrency, and both ambiguity directions.

r1's ``test_concurrent_claims_admit_exactly_one_winner`` called
``try_claim`` twice in a row on one thread. Sequential calls prove
idempotence, not atomicity: a read-then-write implementation passes them
and still loses a real race. The tests here use actual threads, a barrier
so they arrive together, and a hook that forces the interleave a
read-then-write loses -- so removing the store's lock makes them fail
rather than flake.

The second half covers what r1 got backwards in both directions:

* a spawner that raises **after** the session is up was treated as a clean
  failure, so the claim was released and the next tick spawned a duplicate;
* a spawner that returned cleanly having started **nothing** was counted as
  a success, so the claim was held and the fire sat out its lease.

Neither may leak now. The design: prove it (``STARTED``/``NOT_STARTED``),
decide it by readback (``reconcile``), or quarantine it loudly. Each of the
three is tested, including the case where the readback itself fails.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.spawn import (
    ReconcilableSpawner,
    SpawnDisposition,
    SpawnNotStarted,
    SpawnOutcome,
    spawn_key_for,
)

from .conftest import INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit

THREADS = 8


# ---------------------------------------------------------------------------
# 1. Genuine concurrency on the claim primitive
# ---------------------------------------------------------------------------
def test_threaded_claims_admit_exactly_one_winner() -> None:
    """N threads, one event, one winner.

    The hook runs inside ``try_claim`` between the check and the write and
    sleeps, which is precisely the window a read-then-write leaves open.
    Because the store holds its lock across both, the other threads block
    instead of reading stale state -- so this is deterministic, not a race
    the test hopes to catch.
    """
    store = InMemoryClaimStore()
    hook_entered = threading.Event()

    def widen_the_window() -> None:
        # Only the first claimer needs to yield; after that the record
        # exists and the check short-circuits.
        if not hook_entered.is_set():
            hook_entered.set()
            threading.Event().wait(0.05)

    store._race_hook = widen_the_window

    barrier = threading.Barrier(THREADS)
    winners: list[object] = []
    winners_lock = threading.Lock()

    def contend(index: int) -> None:
        barrier.wait()
        claim = store.try_claim(
            event_uuid="evt-1",
            symbol="005930",
            claimed_by=f"racer-{index}",
            now=INCIDENT_TICK,
        )
        if claim is not None:
            with winners_lock:
                winners.append(claim)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "a claimer deadlocked"
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"


def test_threaded_ticks_spawn_one_event_once() -> None:
    """The same race one level up: two ticks, one fire, one spawn."""
    store = InMemoryClaimStore()
    event = make_event(event_uuid="evt-1")

    barrier = threading.Barrier(THREADS)
    spawn_counts: list[int] = []
    counts_lock = threading.Lock()

    def tick() -> None:
        barrier.wait()
        result = run_repricing_tick([event], store=store, now=INCIDENT_TICK)
        with counts_lock:
            spawn_counts.append(len(result.spawned))

    threads = [threading.Thread(target=tick) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "a tick deadlocked"
    assert sum(spawn_counts) == 1, f"one fire spawned {sum(spawn_counts)} sessions"


def test_threaded_ab_race_never_leaves_the_fire_ownerless() -> None:
    """The 08-18 shape as a real race: 0905 rep session vs the 09:06 tick.

    Whoever wins, the event ends up owned -- and owned by exactly one. The
    accident being replayed is the *other* outcome: both consumers deciding
    the other has it.
    """
    store = InMemoryClaimStore()
    event = make_event(event_uuid="evt-005930-276000")
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    lock = threading.Lock()

    def b_plan_rep_session() -> None:
        barrier.wait()
        claim = store.try_claim(
            event_uuid=event.event_uuid,
            symbol="005930",
            claimed_by="kr-open-trade-0905",
            now=INCIDENT_TICK,
        )
        with lock:
            outcomes["b"] = claim is not None

    def a_plan_tick() -> None:
        barrier.wait()
        result = run_repricing_tick([event], store=store, now=INCIDENT_TICK)
        with lock:
            outcomes["a"] = len(result.spawned) == 1

    threads = [
        threading.Thread(target=b_plan_rep_session),
        threading.Thread(target=a_plan_tick),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    # Exactly one consumer took it...
    assert sum(1 for won in outcomes.values() if won) == 1, outcomes
    # ...and it is never left owned by nobody, which is the real accident.
    assert store.state_for(event.event_uuid, now=INCIDENT_TICK) in (
        ConsumptionState.CLAIMED,
        ConsumptionState.CONSUMED,
    )


# ---------------------------------------------------------------------------
# 2. Ambiguity: neither direction may leak
# ---------------------------------------------------------------------------
class _AmbiguousSpawner:
    """Raises a generic error. Whether a session started is unknowable."""

    is_dry = True

    def __init__(self) -> None:
        self.calls = 0

    def spawn(self, request):
        self.calls += 1
        raise TimeoutError("acknowledgement timed out after the request was sent")


class _ReconcilingSpawner(_AmbiguousSpawner):
    """Ambiguous spawn, but can look its own backend up by spawn_key."""

    def __init__(self, verdict: SpawnDisposition) -> None:
        super().__init__()
        self.verdict = verdict
        self.reconciled_keys: list[str] = []

    def reconcile(self, request) -> SpawnDisposition:
        self.reconciled_keys.append(request.spawn_key)
        return self.verdict


def test_ambiguous_spawn_is_not_treated_as_a_clean_failure() -> None:
    """Direction 1: it must NOT release, or the next tick double-spawns."""
    store = InMemoryClaimStore()
    event = make_event(event_uuid="evt-1")
    spawner = _AmbiguousSpawner()

    first = run_repricing_tick([event], store=store, now=INCIDENT_TICK, spawner=spawner)
    second = run_repricing_tick(
        [event],
        store=store,
        now=INCIDENT_TICK + dt.timedelta(minutes=1),
        spawner=spawner,
    )

    assert first.spawned == ()
    assert [s.reason for s in first.needs_reconcile] == ["spawn_ambiguous"]
    assert store.released == []
    # The decisive assertion: the spawner is never asked a second time.
    assert spawner.calls == 1
    assert second.spawned == ()
    assert [s.reason for s in second.skipped] == ["awaiting_spawn_reconcile"]


def test_ambiguous_spawn_is_not_treated_as_a_success() -> None:
    """Direction 2: it must not read as CONSUMED, which would hide a fault."""
    store = InMemoryClaimStore()
    run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=_AmbiguousSpawner(),
    )

    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.QUARANTINED
    assert [state for _, state, _ in store.finalised] == [ConsumptionState.QUARANTINED]


def test_a_quarantined_event_is_reported_and_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Holding the claim is only safe if it is loud. Prove it is loud."""
    store = InMemoryClaimStore()

    with caplog.at_level("ERROR", logger="app.services.watch_trigger_repricing"):
        result = run_repricing_tick(
            [make_event(event_uuid="evt-1")],
            store=store,
            now=INCIDENT_TICK,
            spawner=_AmbiguousSpawner(),
        )

    assert [s.event_uuid for s in result.needs_reconcile] == ["evt-1"]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("AMBIGUOUS" in r.getMessage() for r in errors)
    assert any(spawn_key_for("evt-1") in r.getMessage() for r in errors)
    assert any("NOT be retried automatically" in r.getMessage() for r in errors)
    assert result.as_dict()["needsReconcile"] == [
        {"eventUuid": "evt-1", "symbol": "005930", "reason": "spawn_ambiguous"}
    ]


def test_reconcile_started_consumes_without_a_second_spawn() -> None:
    """Readback says the session is up -> terminal, and never re-spawned."""
    store = InMemoryClaimStore()
    event = make_event(event_uuid="evt-1")
    spawner = _ReconcilingSpawner(SpawnDisposition.STARTED)

    first = run_repricing_tick([event], store=store, now=INCIDENT_TICK, spawner=spawner)
    second = run_repricing_tick(
        [event],
        store=store,
        now=INCIDENT_TICK + dt.timedelta(minutes=1),
        spawner=spawner,
    )

    assert [o.request.event_uuid for o in first.spawned] == ["evt-1"]
    assert first.spawned[0].disposition is SpawnDisposition.STARTED
    assert first.needs_reconcile == ()
    assert spawner.reconciled_keys == [spawn_key_for("evt-1")]
    assert second.spawned == ()
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.CONSUMED


def test_reconcile_not_started_releases_and_retries() -> None:
    """Readback says nothing started -> released, retried, no quarantine."""
    store = InMemoryClaimStore()
    event = make_event(event_uuid="evt-1")
    spawner = _ReconcilingSpawner(SpawnDisposition.NOT_STARTED)

    result = run_repricing_tick(
        [event], store=store, now=INCIDENT_TICK, spawner=spawner
    )

    assert result.spawned == ()
    assert result.needs_reconcile == ()
    assert [s.reason for s in result.skipped] == ["spawn_not_started"]
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNCLAIMED

    retried = run_repricing_tick(
        [event],
        store=store,
        now=INCIDENT_TICK + dt.timedelta(minutes=1),
        spawner=_ReconcilingSpawner(SpawnDisposition.STARTED),
    )
    assert [o.request.event_uuid for o in retried.spawned] == ["evt-1"]


def test_a_failing_reconcile_leaves_the_ambiguity_standing() -> None:
    """A broken readback must not decay into 'not started'."""

    class BrokenReconciler(_AmbiguousSpawner):
        def reconcile(self, request) -> SpawnDisposition:
            raise ConnectionError("session registry unreachable")

    store = InMemoryClaimStore()
    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=BrokenReconciler(),
    )

    assert [s.reason for s in result.needs_reconcile] == ["spawn_ambiguous"]
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.QUARANTINED
    assert store.released == []


def test_an_inconclusive_reconcile_also_quarantines() -> None:
    """``reconcile`` returning AMBIGUOUS is an answer, and it is 'unknown'."""
    store = InMemoryClaimStore()
    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=_ReconcilingSpawner(SpawnDisposition.AMBIGUOUS),
    )

    assert [s.reason for s in result.needs_reconcile] == ["spawn_ambiguous"]
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.QUARANTINED


def test_explicit_not_started_return_is_not_counted_as_spawned() -> None:
    """A clean return that started nothing is a failure, not a success."""

    class HonestNoOpSpawner:
        is_dry = True

        def spawn(self, request):
            return SpawnOutcome(
                request=request,
                disposition=SpawnDisposition.NOT_STARTED,
                detail="backend at capacity",
            )

    store = InMemoryClaimStore()
    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=HonestNoOpSpawner(),
    )

    assert result.spawned == ()
    assert [s.reason for s in result.skipped] == ["spawn_not_started"]
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNCLAIMED


def test_spawn_not_started_is_the_only_clean_failure_exception() -> None:
    """The two exception paths must diverge -- that is the whole fix."""
    clean_store = InMemoryClaimStore()
    ambiguous_store = InMemoryClaimStore()

    class CleanFailure:
        is_dry = True

        def spawn(self, request):
            raise SpawnNotStarted("refused before dispatch")

    run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=clean_store,
        now=INCIDENT_TICK,
        spawner=CleanFailure(),
    )
    run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=ambiguous_store,
        now=INCIDENT_TICK,
        spawner=_AmbiguousSpawner(),
    )

    assert (
        clean_store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNCLAIMED
    )
    assert (
        ambiguous_store.state_for("evt-1", now=INCIDENT_TICK)
        is ConsumptionState.QUARANTINED
    )


# ---------------------------------------------------------------------------
# 3. The identity that makes readback possible
# ---------------------------------------------------------------------------
def test_spawn_key_is_stable_across_ticks_and_carries_no_clock() -> None:
    """A key that varied per attempt could not find the earlier session."""
    early = make_event(event_uuid="evt-1")
    store_a, store_b = InMemoryClaimStore(), InMemoryClaimStore()

    first = run_repricing_tick([early], store=store_a, now=INCIDENT_TICK)
    later = run_repricing_tick(
        [early], store=store_b, now=INCIDENT_TICK + dt.timedelta(hours=3)
    )

    assert first.spawned[0].request.spawn_key == later.spawned[0].request.spawn_key
    assert first.spawned[0].request.spawn_key == spawn_key_for("evt-1")
    # The labels differ (they carry the clock); the key must not.
    assert first.spawned[0].request.label != later.spawned[0].request.label


def test_reconcilable_protocol_is_detected_structurally() -> None:
    assert isinstance(
        _ReconcilingSpawner(SpawnDisposition.STARTED), ReconcilableSpawner
    )
    assert not isinstance(_AmbiguousSpawner(), ReconcilableSpawner)
