"""ROB-1286 B2 + r2 NEW BLOCKER 1/2 — fencing and same-symbol exclusion.

r2 reproduced two escapes against the in-memory store:

    B2_STALE_OWNER spawn_calls=[('A', 'evt-stale'), ('B', 'evt-stale')]
    SYMBOL_RACE results=[('evt-rung-a', 1), ('evt-rung-b', 1)] total_spawned=2

Both are now closed by construction -- generation + owner token for the
first, an active-symbol uniqueness rule for the second -- and these tests
drive real concurrency rather than sequential calls, because the previous
round's "atomic" claim passed sequential tests and failed a barrier.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    ClaimNotHeld,
    InMemoryClaimStore,
)
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.lifecycle import (
    ClaimLifecycle,
    proposal_created,
    rejected,
)

from .conftest import INCIDENT_TICK

pytestmark = pytest.mark.unit


async def _claim(store, *, event, symbol, who, now=INCIDENT_TICK):
    return await store.try_claim(
        event_uuid=event, symbol=symbol, market="kr", claimed_by=who, now=now
    )


# ---------------------------------------------------------------------------
# NEW BLOCKER 1 — stale claimant fencing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rolled_over_claimant_cannot_finalise_the_new_one() -> None:
    store = InMemoryClaimStore()
    stale = await _claim(store, event="evt-1", symbol="005930", who="A")
    assert stale is not None

    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)
    fresh = await _claim(store, event="evt-1", symbol="005930", who="B", now=after)
    assert fresh is not None
    assert fresh.generation == stale.generation + 1

    # A comes back late and tries to close the event.
    with pytest.raises(ClaimNotHeld):
        await store.finalise(stale, proposal_created("p-from-stale-owner"))

    # B still owns it and can finish normally.
    await store.finalise(fresh, proposal_created("p-from-current-owner"))
    live = [c for c in store.snapshot() if c.generation == fresh.generation]
    assert live[0].proposal_id == "p-from-current-owner"


@pytest.mark.asyncio
async def test_stale_owner_cannot_release_the_new_claim_either() -> None:
    """Release is fenced too -- otherwise it becomes a free re-spawn."""
    store = InMemoryClaimStore()
    stale = await _claim(store, event="evt-1", symbol="005930", who="A")
    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)
    await _claim(store, event="evt-1", symbol="005930", who="B", now=after)

    with pytest.raises(ClaimNotHeld):
        await store.release(stale, reason="late cleanup")


@pytest.mark.asyncio
async def test_ttl_rollover_records_the_expiry_terminal() -> None:
    """The rollover leaves an audit row, not a silent slot reuse."""
    store = InMemoryClaimStore()
    await _claim(store, event="evt-1", symbol="005930", who="A")

    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)
    await _claim(store, event="evt-1", symbol="005930", who="B", now=after)

    states = {c.generation: c.state for c in store.snapshot()}
    assert states[1] is ClaimLifecycle.EXPIRED_UNPROCESSED
    assert states[2] is ClaimLifecycle.STARTED


# ---------------------------------------------------------------------------
# NEW BLOCKER 2 — same symbol, different events
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_different_events_on_one_symbol_admit_one_winner() -> None:
    """The 08-18 shape: ladder rungs 1 and 2 both fire on 005930."""
    store = InMemoryClaimStore()

    a = await _claim(store, event="evt-rung-a", symbol="005930", who="tickA")
    b = await _claim(store, event="evt-rung-b", symbol="005930", who="tickB")

    assert (a is None) != (b is None), "exactly one claimant must win"


@pytest.mark.asyncio
async def test_same_symbol_race_under_real_concurrency() -> None:
    """Both coroutines start from the same empty view, as in r2's repro."""
    store = InMemoryClaimStore()
    barrier = asyncio.Barrier(2)

    async def contend(event: str):
        await barrier.wait()
        return await _claim(store, event=event, symbol="005930", who=event)

    results = await asyncio.gather(contend("evt-a"), contend("evt-b"))
    winners = [handle for handle in results if handle is not None]

    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"


@pytest.mark.asyncio
async def test_same_event_race_under_real_concurrency() -> None:
    store = InMemoryClaimStore()
    barrier = asyncio.Barrier(8)

    async def contend(index: int):
        await barrier.wait()
        return await _claim(store, event="evt-1", symbol="005930", who=f"w{index}")

    results = await asyncio.gather(*(contend(i) for i in range(8)))
    winners = [handle for handle in results if handle is not None]

    assert len(winners) == 1


@pytest.mark.asyncio
async def test_a_different_symbol_is_not_blocked() -> None:
    """The rule is per symbol, not a global lock."""
    store = InMemoryClaimStore()
    a = await _claim(store, event="evt-a", symbol="005930", who="t")
    b = await _claim(store, event="evt-b", symbol="000660", who="t")

    assert a is not None and b is not None


@pytest.mark.asyncio
async def test_symbol_frees_once_the_event_reaches_a_terminal() -> None:
    store = InMemoryClaimStore()
    first = await _claim(store, event="evt-a", symbol="005930", who="t")
    await store.finalise(first, rejected("no sellable quantity"))

    second = await _claim(store, event="evt-b", symbol="005930", who="t")
    assert second is not None


# ---------------------------------------------------------------------------
# A resolved event is never re-judged
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_resolved_event_is_not_reclaimable() -> None:
    """Re-judging a fire that already produced a proposal is the double
    proposal direction, and no lease expiry may walk it back."""
    store = InMemoryClaimStore()
    handle = await _claim(store, event="evt-1", symbol="005930", who="A")
    await store.finalise(handle, proposal_created("p-1"))

    long_after = INCIDENT_TICK + DEFAULT_LEASE * 10
    again = await _claim(store, event="evt-1", symbol="005930", who="B", now=long_after)

    assert again is None
    assert await store.state_for("evt-1", now=long_after) is ConsumptionState.CONSUMED


@pytest.mark.asyncio
async def test_an_expired_event_is_reclaimable_and_reads_unclaimed() -> None:
    """TTL means nobody judged it -- it must come back, not stay buried."""
    store = InMemoryClaimStore()
    await _claim(store, event="evt-1", symbol="005930", who="A")

    after = INCIDENT_TICK + DEFAULT_LEASE + dt.timedelta(minutes=1)
    assert await store.state_for("evt-1", now=after) is ConsumptionState.UNCLAIMED
    assert (
        await _claim(store, event="evt-1", symbol="005930", who="B", now=after)
        is not None
    )


@pytest.mark.asyncio
async def test_unavailable_store_is_unknown_not_unclaimed() -> None:
    store = InMemoryClaimStore()
    store.available = False

    assert await store.state_for("evt-1", now=INCIDENT_TICK) is ConsumptionState.UNKNOWN
