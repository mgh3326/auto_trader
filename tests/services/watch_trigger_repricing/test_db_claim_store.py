"""ROB-1286 B1/B2 — the durable claim store, against a real database.

r2's finding was that dedup lived in a process singleton, so two Prefect
runs each saw an unclaimed fire:

    process 1: status=ok spawned=1
    process 2: status=ok spawned=1

These tests use separate sessions against the run-owned test database, so
the exclusion being exercised is the one the deployment relies on: two
database constraints, not a shared Python object.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import engine
from app.models.watch_event_repricing_claims import WatchEventRepricingClaim
from app.services.watch_trigger_repricing.claims import (
    DEFAULT_LEASE,
    ClaimNotHeld,
)
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.db_claim_store import DatabaseClaimStore
from app.services.watch_trigger_repricing.lifecycle import (
    ClaimLifecycle,
    proposal_created,
    rejected,
)
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 18, 0, 6, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def store(_bootstrap_test_schema):
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await session.execute(delete(WatchEventRepricingClaim))
        await session.commit()
    yield DatabaseClaimStore(session_factory=AsyncSessionLocal)
    async with AsyncSessionLocal() as session:
        await session.execute(delete(WatchEventRepricingClaim))
        await session.commit()


def _uuid(n: int) -> str:
    return str(uuid.UUID(int=n))


async def _claim(store, *, event, symbol, who, now=NOW):
    return await store.try_claim(
        event_uuid=event, symbol=symbol, market="kr", claimed_by=who, now=now
    )


# ---------------------------------------------------------------------------
# The store is durable by type, not by self-report
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_store_reports_durable(store) -> None:
    assert store.is_durable is True


# ---------------------------------------------------------------------------
# B1 — dedup survives separate sessions (the run boundary)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_same_event_claimed_once_across_sessions(store) -> None:
    first = await _claim(store, event=_uuid(1), symbol="005930", who="run-1")
    second = await _claim(store, event=_uuid(1), symbol="005930", who="run-2")

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_resolved_event_is_never_reclaimed(store) -> None:
    handle = await _claim(store, event=_uuid(2), symbol="005930", who="run-1")
    await store.finalise(handle, proposal_created("prop-abc"))

    long_after = NOW + DEFAULT_LEASE * 10
    again = await _claim(
        store, event=_uuid(2), symbol="005930", who="run-2", now=long_after
    )

    assert again is None
    assert await store.state_for(_uuid(2), now=long_after) is ConsumptionState.CONSUMED


# ---------------------------------------------------------------------------
# B2 / NEW BLOCKER 2 — the per-symbol rule is a database constraint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_events_one_symbol_admit_one_winner(store) -> None:
    a = await _claim(store, event=_uuid(3), symbol="005930", who="tickA")
    b = await _claim(store, event=_uuid(4), symbol="005930", who="tickB")

    assert (a is None) != (b is None)


@pytest.mark.asyncio
async def test_different_symbols_both_claimable(store) -> None:
    a = await _claim(store, event=_uuid(5), symbol="005930", who="t")
    b = await _claim(store, event=_uuid(6), symbol="000660", who="t")

    assert a is not None and b is not None


@pytest.mark.asyncio
async def test_symbol_frees_after_a_terminal(store) -> None:
    first = await _claim(store, event=_uuid(7), symbol="005930", who="t")
    await store.finalise(first, rejected("sellable qty is 0"))

    second = await _claim(store, event=_uuid(8), symbol="005930", who="t")
    assert second is not None


# ---------------------------------------------------------------------------
# NEW BLOCKER 1 — fencing across a lease rollover
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stale_owner_cannot_finalise_after_rollover(store) -> None:
    stale = await _claim(store, event=_uuid(9), symbol="005930", who="A")
    after = NOW + DEFAULT_LEASE + dt.timedelta(minutes=1)
    fresh = await _claim(store, event=_uuid(9), symbol="005930", who="B", now=after)

    assert fresh is not None
    assert fresh.generation == stale.generation + 1

    with pytest.raises(ClaimNotHeld):
        await store.finalise(stale, proposal_created("p-stale"))

    await store.finalise(fresh, proposal_created("p-current"))
    outcomes = await store.outcomes_for([_uuid(9)])
    assert outcomes[_uuid(9)].proposal_id == "p-current"


@pytest.mark.asyncio
async def test_rollover_records_the_expiry_terminal(store) -> None:
    """The TTL path leaves an audit row saying the fire went unjudged."""
    await _claim(store, event=_uuid(10), symbol="005930", who="A")
    after = NOW + DEFAULT_LEASE + dt.timedelta(minutes=1)
    await _claim(store, event=_uuid(10), symbol="005930", who="B", now=after)

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        rows = (
            await session.scalars(
                select(WatchEventRepricingClaim)
                .where(WatchEventRepricingClaim.event_uuid == uuid.UUID(_uuid(10)))
                .order_by(WatchEventRepricingClaim.generation)
            )
        ).all()

    assert [r.state for r in rows] == [
        ClaimLifecycle.EXPIRED_UNPROCESSED.value,
        ClaimLifecycle.STARTED.value,
    ]


@pytest.mark.asyncio
async def test_sweep_expired_names_the_unjudged_fires(store) -> None:
    await _claim(store, event=_uuid(11), symbol="005930", who="A")
    after = NOW + DEFAULT_LEASE + dt.timedelta(minutes=1)

    expired = await store.sweep_expired(now=after)

    assert expired == [_uuid(11)]
    # And it reads as unclaimed again, because nobody judged it.
    assert await store.state_for(_uuid(11), now=after) is ConsumptionState.UNCLAIMED


# ---------------------------------------------------------------------------
# The database refuses a terminal without evidence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_rejects_an_analysis_only_terminal(store) -> None:
    """Belt and braces: the CHECK constraint, not just the enum."""
    from sqlalchemy.exc import IntegrityError

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        session.add(
            WatchEventRepricingClaim(
                event_uuid=uuid.UUID(_uuid(12)),
                symbol="005930",
                market="kr",
                generation=1,
                owner_token=uuid.uuid4(),
                claimed_by="t",
                state="analysed",
                lease_expires_at=NOW + DEFAULT_LEASE,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_proposal_terminal_without_proposal_id(store) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        session.add(
            WatchEventRepricingClaim(
                event_uuid=uuid.UUID(_uuid(13)),
                symbol="005930",
                market="kr",
                generation=1,
                owner_token=uuid.uuid4(),
                claimed_by="t",
                state="proposal_created",
                lease_expires_at=NOW + DEFAULT_LEASE,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# ---------------------------------------------------------------------------
# ROB-1290 — release, which the durable store never implemented
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_hands_the_fire_back_and_the_next_tick_can_reclaim(
    store,
) -> None:
    """The branch no shipped spawner could reach until the chain existed.

    ``release`` was on the ClaimStore protocol and only the in-memory
    rehearsal store implemented it, so the first spawner able to prove a
    clean failure would have raised ``AttributeError`` mid-tick.
    """
    event = _uuid(20)
    handle = await store.try_claim(
        event_uuid=event, symbol="005930", market="kr", claimed_by="t", now=NOW
    )
    assert handle is not None

    await store.release(handle, reason="spawn_not_started")

    # The fire looks untouched ...
    assert await store.state_for(event, now=NOW) is ConsumptionState.UNCLAIMED
    # ... the symbol slot is free ...
    assert await store.active_symbols(now=NOW) == frozenset()
    # ... and a later tick can take it for real.
    again = await store.try_claim(
        event_uuid=event, symbol="005930", market="kr", claimed_by="t2", now=NOW
    )
    assert again is not None
    await store.finalise(again, proposal_created("pid-20"))
    outcomes = await store.outcomes_for([event])
    assert outcomes[event].state is ClaimLifecycle.PROPOSAL_CREATED


@pytest.mark.asyncio
async def test_release_is_fenced_against_a_stale_owner(store) -> None:
    event = _uuid(21)
    handle = await store.try_claim(
        event_uuid=event, symbol="000660", market="kr", claimed_by="t", now=NOW
    )
    assert handle is not None
    await store.finalise(handle, rejected("judged and declined"))

    # The same handle must not be able to delete its own terminal.
    with pytest.raises(ClaimNotHeld):
        await store.release(handle, reason="spawn_not_started")
    outcomes = await store.outcomes_for([event])
    assert outcomes[event].state is ClaimLifecycle.REJECTED_WITH_REASON
