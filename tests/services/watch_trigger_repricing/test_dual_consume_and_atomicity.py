"""ROB-1286 §3 / AC2-AC3 — dual consumption, races, claim-spawn atomicity.

The failure this guards against is not "a duplicate row". It is: one watch
fire becoming two independent sell proposals, or -- in the other
direction -- being marked handled by nobody and disappearing, which is the
accident the issue exists to fix.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    ClaimStoreUnavailable,
    InMemoryClaimStore,
)
from app.services.watch_trigger_repricing.consumption import (
    CONSUMABLE_OUTCOMES,
    ConsumptionState,
    is_consumable_outcome,
    may_consume,
    project_claim_state,
)
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.selection import select_candidates
from app.services.watch_trigger_repricing.spawn import SpawnNotStarted

from .conftest import INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The canonical criterion (both consumers are defined against this)
# ---------------------------------------------------------------------------
def test_unreachable_store_is_unknown_not_unclaimed() -> None:
    """'Lookup found nothing' must never be read as 'nobody owns it'."""
    assert (
        project_claim_state(claim_found=False, store_available=False)
        is ConsumptionState.UNKNOWN
    )
    assert (
        project_claim_state(claim_found=False, store_available=True)
        is ConsumptionState.UNCLAIMED
    )
    assert (
        project_claim_state(claim_found=True, store_available=True)
        is ConsumptionState.CLAIMED
    )


def test_only_proven_unclaimed_may_be_consumed() -> None:
    assert may_consume(ConsumptionState.UNCLAIMED) is True
    assert may_consume(ConsumptionState.CLAIMED) is False
    assert may_consume(ConsumptionState.UNKNOWN) is False


def test_only_review_required_is_consumable() -> None:
    """Widening this set widens what reaches order_proposal_create."""
    assert CONSUMABLE_OUTCOMES == frozenset({"review_required"})
    assert is_consumable_outcome("review_required") is True
    for other in ("notified", "preview_attached", "executed", "expired", None):
        assert is_consumable_outcome(other) is False


# ---------------------------------------------------------------------------
# A안 x B안: the two consumers cannot both take one event
# ---------------------------------------------------------------------------
def test_b_plan_claim_blocks_a_plan_spawn(store: InMemoryClaimStore) -> None:
    """B안 (rep session) got there first -> A안 must not spawn."""
    event = make_event(event_uuid="evt-1")
    store.try_claim(
        event_uuid="evt-1",
        symbol=event.symbol,
        claimed_by="kr-open-trade-session",
        now=INCIDENT_TICK,
    )

    result = run_repricing_tick([event], store=store, now=INCIDENT_TICK)

    assert result.spawned == ()
    assert [s.reason for s in result.skipped] == ["already_consumed"]


def test_a_plan_claim_is_visible_to_b_plan(store: InMemoryClaimStore) -> None:
    """A안 got there first -> the same criterion tells B안 to stand down."""
    event = make_event(event_uuid="evt-1")
    run_repricing_tick([event], store=store, now=INCIDENT_TICK)

    # B안 asks the identical question through the identical helper. r2: the
    # verdict is the terminal CONSUMED, so B안 still stands down half an
    # hour later -- under r1 the lease had lapsed by then and B안 (and the
    # next tick) would have re-judged the same fire.
    state = store.state_for("evt-1", now=INCIDENT_TICK)
    assert state is ConsumptionState.CONSUMED
    assert may_consume(state) is False

    much_later = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(hours=1)
    assert store.state_for("evt-1", now=much_later) is ConsumptionState.CONSUMED
    assert may_consume(store.state_for("evt-1", now=much_later)) is False


def test_neither_consumer_skips_an_untouched_event(store: InMemoryClaimStore) -> None:
    """The 'both skip' direction is the original accident. Guard it too."""
    state = store.state_for("evt-never-seen", now=INCIDENT_TICK)
    assert state is ConsumptionState.UNCLAIMED
    assert may_consume(state) is True


def test_concurrent_claims_admit_exactly_one_winner(
    store: InMemoryClaimStore,
) -> None:
    first = store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="a", now=INCIDENT_TICK
    )
    second = store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="b", now=INCIDENT_TICK
    )

    assert first is not None
    assert second is None


# ---------------------------------------------------------------------------
# Atomicity: claim precedes spawn, and the lease closes the crash window
# ---------------------------------------------------------------------------
def test_claim_is_taken_before_spawn(store: InMemoryClaimStore) -> None:
    """Proven by observing the claim from inside the spawner."""
    observed: list[ConsumptionState] = []

    class ObservingSpawner:
        is_dry = True

        def spawn(self, request):
            observed.append(store.state_for(request.event_uuid, now=INCIDENT_TICK))
            from app.services.watch_trigger_repricing.spawn import (
                SpawnDisposition,
                SpawnOutcome,
            )

            return SpawnOutcome(
                request=request,
                disposition=SpawnDisposition.DRY,
                detail="dry_run",
            )

    run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=ObservingSpawner(),
    )

    assert observed == [ConsumptionState.CLAIMED]


def test_proven_failed_spawn_releases_the_claim(store: InMemoryClaimStore) -> None:
    """An orderly failure hands the fire straight back, with a reason.

    r2: the spawner must *prove* it started nothing, by raising
    ``SpawnNotStarted``. A generic exception is ambiguous and is handled in
    ``test_atomicity_concurrency.py`` -- treating it as a clean failure was
    BLOCKER-2's double-spawn direction.
    """

    class CleanlyFailingSpawner:
        is_dry = True

        def spawn(self, request):
            raise SpawnNotStarted("spawn backend refused the request")

    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=store,
        now=INCIDENT_TICK,
        spawner=CleanlyFailingSpawner(),
    )

    assert result.spawned == ()
    assert [s.reason for s in result.skipped] == ["spawn_not_started"]
    assert store.released == [("evt-1", "spawn_not_started")]
    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNCLAIMED


def test_crash_between_claim_and_spawn_self_heals_via_lease(
    store: InMemoryClaimStore,
) -> None:
    """The residual window is bounded latency, not a lost fire."""
    store.try_claim(
        event_uuid="evt-1",
        symbol="005930",
        claimed_by="tick-that-died",
        now=INCIDENT_TICK,
    )

    during = INCIDENT_TICK + DEFAULT_LEASE - dt.timedelta(minutes=1)
    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)

    assert store.state_for("evt-1", now=during) is ConsumptionState.CLAIMED
    assert store.state_for("evt-1", now=after) is ConsumptionState.UNCLAIMED

    revived = run_repricing_tick(
        [make_event(event_uuid="evt-1")], store=store, now=after
    )
    assert [o.request.event_uuid for o in revived.spawned] == ["evt-1"]


def test_unavailable_store_fails_closed_and_says_so(
    store: InMemoryClaimStore,
) -> None:
    """No spawn without proof, and no silence about why."""
    store.available = False

    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")], store=store, now=INCIDENT_TICK
    )

    assert result.spawned == ()
    assert [s.reason for s in result.skipped] == ["claim_store_unavailable"]


def test_selection_does_not_claim(store: InMemoryClaimStore) -> None:
    """Selection reads; only the orchestrator writes."""
    select_candidates([make_event(event_uuid="evt-1")], store=store, now=INCIDENT_TICK)

    assert store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNCLAIMED


def test_active_symbols_raises_rather_than_reporting_empty(
    store: InMemoryClaimStore,
) -> None:
    """An empty set from a dead store would read as 'nothing in flight'."""
    store.try_claim(
        event_uuid="evt-1", symbol="005930", claimed_by="a", now=INCIDENT_TICK
    )
    store.available = False

    with pytest.raises(ClaimStoreUnavailable):
        store.active_symbols(now=INCIDENT_TICK)
