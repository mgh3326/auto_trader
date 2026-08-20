"""ROB-1290 — poll -> claim -> spawn -> ``order_proposal_create``, for real.

What ROB-1286's e2e could not show
----------------------------------
Its ``test_full_chain_reaches_a_real_proposal_row`` created the proposal
row *itself*, before the tick, and handed the id to a scripted spawner. So
the row existed whether or not the tick worked, and the assertion "the id
in the mapping is a real row" was true by construction.

These tests invert that. Every one of them asserts the proposal table is
**empty for this lane before the tick runs**, and the rows that exist
afterwards were created by the spawn path calling the real
``order_proposal_create`` against the run-owned test database. Delete the
call and there is no row to assert on.

The one thing still injected is the *judgement* -- whether a fired level
still deserves an order. That is LLM work and this runtime owns no
in-process LLM provider, so it was always going to be a port. Everything
downstream of the decision is the shipping chain.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.models.investment_reports import InvestmentWatchEvent
from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalDispatchAttempt,
    OrderProposalRung,
)
from app.models.watch_event_repricing_claims import WatchEventRepricingClaim
from app.services.watch_trigger_repricing.chain_spawner import ProposalChainSpawner
from app.services.watch_trigger_repricing.claims import DEFAULT_LEASE
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.db_claim_store import DatabaseClaimStore
from app.services.watch_trigger_repricing.entrypoint import run_watch_repricing_tick
from app.services.watch_trigger_repricing.judgement import (
    Decline,
    ProposalDraft,
    ProposalRung,
)
from app.services.watch_trigger_repricing.lifecycle import ClaimLifecycle
from app.services.watch_trigger_repricing.spawn import SpawnRequest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("enabled")]

KST = dt.timezone(dt.timedelta(hours=9))
FIRE = dt.datetime(2026, 8, 18, 9, 5, tzinfo=KST)
TICK = dt.datetime(2026, 8, 18, 9, 6, tzinfo=KST)

PROPOSER = "rob1290-chain"
CORRELATION_PREFIX = "rob1290-chain"

# Three distinct symbols, so one tick can judge all three: the per-symbol
# concurrency rule is a real constraint and this test is about the chain,
# not about deferral (which ROB-1286's suite already covers).
FIRES: tuple[tuple[str, str, int], ...] = (
    (str(uuid.UUID(int=101290001)), "005930", 276000),
    (str(uuid.UUID(int=101290002)), "000660", 198000),
    (str(uuid.UUID(int=101290003)), "035420", 214000),
)
BY_SYMBOL = {symbol: event_uuid for event_uuid, symbol, _ in FIRES}


class ScriptedJudge:
    """Answers with a pre-decided judgement. Decide-only: it never writes.

    Stands in for the out-of-process re-judgement session, and *only* for
    it -- the draft it returns is turned into a real proposal row by the
    shipping write seam, not by this class.
    """

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def judge(self, request: SpawnRequest) -> object:
        self.asked.append(request.event_uuid)
        return self.answers.get(request.event_uuid)


def sell_draft(event_uuid: str, symbol: str, *, limit_price: str) -> ProposalDraft:
    return ProposalDraft(
        event_uuid=event_uuid,
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        rungs=(
            ProposalRung(
                rung_index=0, side="sell", quantity="10", limit_price=limit_price
            ),
        ),
        thesis=f"watch level crossed on {symbol}; trim into resistance",
    )


async def _clean(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(delete(WatchEventRepricingClaim))
        await session.execute(
            delete(InvestmentWatchEvent).where(
                InvestmentWatchEvent.correlation_id.like(f"{CORRELATION_PREFIX}%")
            )
        )
        mine = select(OrderProposal.id).where(OrderProposal.proposer == PROPOSER)
        await session.execute(
            delete(OrderProposalApprovalDispatchAttempt).where(
                OrderProposalApprovalDispatchAttempt.proposal_pk.in_(mine)
            )
        )
        await session.execute(
            delete(OrderProposalRung).where(OrderProposalRung.proposal_pk.in_(mine))
        )
        await session.execute(
            delete(OrderProposal).where(OrderProposal.proposer == PROPOSER)
        )
        await session.commit()


@pytest_asyncio.fixture
async def seeded(_bootstrap_test_schema):
    from app.core.db import AsyncSessionLocal

    await _clean(AsyncSessionLocal)
    async with AsyncSessionLocal() as session:
        for index, (event_uuid, symbol, threshold) in enumerate(FIRES, start=1):
            session.add(
                InvestmentWatchEvent(
                    event_uuid=uuid.UUID(event_uuid),
                    idempotency_key=f"{CORRELATION_PREFIX}:{event_uuid}",
                    market="kr",
                    target_kind="asset",
                    symbol=symbol,
                    metric="price",
                    operator="above",
                    threshold=threshold,
                    threshold_key=f"price_above_{threshold}",
                    intent="sell_review",
                    action_mode="approval_required",
                    outcome="review_required",
                    correlation_id=f"{CORRELATION_PREFIX}-{index}",
                    kst_date="2026-08-18",
                    delivery_status="delivered",
                    delivered_at=FIRE,
                )
            )
        await session.commit()
    yield AsyncSessionLocal
    await _clean(AsyncSessionLocal)


async def proposal_ids(session_factory) -> set[str]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(OrderProposal.proposal_id).where(OrderProposal.proposer == PROPOSER)
        )
        return {str(row) for row in rows}


async def proposal_count(session_factory) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(OrderProposal)
                .where(OrderProposal.proposer == PROPOSER)
            )
            or 0
        )


def spawner_for(answers: dict[str, object]) -> ProposalChainSpawner:
    return ProposalChainSpawner(judge=ScriptedJudge(answers), proposer=PROPOSER)


def unsupported_draft(event_uuid: str, symbol: str) -> ProposalDraft:
    """A draft the real boundary refuses *before* it commits anything.

    ``upbit`` cannot trade ``equity_kr``, and ``order_proposal_create``
    rejects the combination in its pre-commit validation, returning
    ``success: False``. No fake tool is involved: this is the real
    refusal, which is what makes "provably no row" evidence rather than
    an assumption.
    """
    return ProposalDraft(
        event_uuid=event_uuid,
        symbol=symbol,
        market="equity_kr",
        account_mode="upbit",
        side="sell",
        order_type="limit",
        rungs=(
            ProposalRung(
                rung_index=0, side="sell", quantity="10", limit_price="286000"
            ),
        ),
        thesis="deliberately unsupported account_mode/market pair",
    )


# ---------------------------------------------------------------------------
# GAP_CLOSED / REAL_CHAIN — the tick creates the row, nothing else does
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_tick_itself_creates_the_proposal_row(seeded) -> None:
    """No row is planted first. If the spawn path does not call the tool,
    there is nothing here to find."""
    assert await proposal_count(seeded) == 0, "precondition: this lane owns no rows"

    event_uuid, symbol, _ = FIRES[0]
    spawner = spawner_for(
        {
            event_uuid: sell_draft(event_uuid, symbol, limit_price="286000"),
            BY_SYMBOL["000660"]: Decline(
                event_uuid=BY_SYMBOL["000660"], reason="level reclaimed before judging"
            ),
            BY_SYMBOL["035420"]: Decline(
                event_uuid=BY_SYMBOL["035420"], reason="no sellable quantity"
            ),
        }
    )

    result = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded), spawner=spawner, now=TICK
    )

    assert result["status"] == "ok"
    created = await proposal_ids(seeded)
    assert len(created) == 1, "exactly the one draft became a row"

    mapping = {row["eventUuid"]: row for row in result["completion"]}
    assert mapping[event_uuid]["state"] == ClaimLifecycle.PROPOSAL_CREATED
    # The id in the mapping is the id of the row the tick wrote.
    assert mapping[event_uuid]["proposalId"] in created


@pytest.mark.asyncio
async def test_the_created_row_carries_the_draft_and_lane_provenance(seeded) -> None:
    event_uuid, symbol, _ = FIRES[0]
    spawner = spawner_for(
        {event_uuid: sell_draft(event_uuid, symbol, limit_price="286000")}
    )
    await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded), spawner=spawner, now=TICK
    )

    created = await proposal_ids(seeded)
    assert len(created) == 1
    async with seeded() as session:
        row = await session.scalar(
            select(OrderProposal).where(
                OrderProposal.proposal_id == uuid.UUID(created.pop())
            )
        )
    assert row is not None
    assert row.symbol == symbol
    assert row.side == "sell"
    assert row.proposer == PROPOSER
    assert row.rationale["event_uuid"] == event_uuid
    assert row.rationale["source"] == "watch_trigger_repricing"


# ---------------------------------------------------------------------------
# COMPLETION — N fires in, N outcomes out, one each
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_fire_maps_to_exactly_one_proposal_or_one_reason(seeded) -> None:
    assert await proposal_count(seeded) == 0

    answers: dict[str, object] = {
        BY_SYMBOL["005930"]: sell_draft(
            BY_SYMBOL["005930"], "005930", limit_price="286000"
        ),
        BY_SYMBOL["000660"]: sell_draft(
            BY_SYMBOL["000660"], "000660", limit_price="205000"
        ),
        BY_SYMBOL["035420"]: Decline(
            event_uuid=BY_SYMBOL["035420"],
            reason="level reclaimed within the bar; no trim warranted",
        ),
    }
    result = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded),
        spawner=spawner_for(answers),
        now=TICK,
    )

    polled = {row["eventUuid"] for row in result["polled"]}
    assert polled == {event_uuid for event_uuid, _, _ in FIRES}

    rows = {row["eventUuid"]: row for row in result["completion"]}
    assert set(rows) == polled, "the mapping covers the polled set exactly"

    # The 1:1 assertion: exactly one of the two, for every fire, no blanks.
    for event_uuid, row in rows.items():
        has_proposal = bool((row["proposalId"] or "").strip())
        has_reason = bool((row["rejectionReason"] or "").strip())
        assert has_proposal != has_reason, (
            f"event {event_uuid} resolved to {row} -- every fire must end as a "
            "proposal or as an attributed reason, and never as both or neither"
        )

    assert result["completionComplete"] is True
    assert result["completionAccounted"] is True

    # And the two proposal ids are two distinct rows the tick actually wrote.
    created = await proposal_ids(seeded)
    assert len(created) == 2
    assert {rows[u]["proposalId"] for u in rows if rows[u]["proposalId"]} == created


@pytest.mark.asyncio
async def test_the_terminals_are_persisted_against_the_real_ids(seeded) -> None:
    answers: dict[str, object] = {
        BY_SYMBOL["005930"]: sell_draft(
            BY_SYMBOL["005930"], "005930", limit_price="286000"
        ),
        BY_SYMBOL["000660"]: Decline(
            event_uuid=BY_SYMBOL["000660"], reason="already trimmed this session"
        ),
    }
    store = DatabaseClaimStore(session_factory=seeded)
    await run_watch_repricing_tick(store=store, spawner=spawner_for(answers), now=TICK)

    created = await proposal_ids(seeded)
    outcomes = await store.outcomes_for([BY_SYMBOL["005930"], BY_SYMBOL["000660"]])

    assert outcomes[BY_SYMBOL["005930"]].state is ClaimLifecycle.PROPOSAL_CREATED
    assert outcomes[BY_SYMBOL["005930"]].proposal_id in created
    assert outcomes[BY_SYMBOL["000660"]].state is ClaimLifecycle.REJECTED_WITH_REASON
    assert (
        outcomes[BY_SYMBOL["000660"]].rejection_reason == "already trimmed this session"
    )


@pytest.mark.asyncio
async def test_an_analysis_only_session_leaves_the_fire_unresolved(seeded) -> None:
    """The mutant AC3 asks about: a judge that produces nothing must go RED.

    Nothing is written, the run is not complete, and the fire is handed
    back rather than being marked done -- so a later tick can still judge
    it. What must never happen is a blank row that reads as success.
    """
    result = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded),
        spawner=spawner_for({}),  # every judge answer is None
        now=TICK,
    )

    assert await proposal_count(seeded) == 0
    assert result["completionComplete"] is False

    rows = {row["eventUuid"]: row for row in result["completion"]}
    assert set(rows) == {event_uuid for event_uuid, _, _ in FIRES}
    for row in rows.values():
        assert not row["proposalId"]
        assert not row["rejectionReason"]
        # Not silently omitted: each carries the reason it was set aside.
        assert row["deferralReason"], row
    assert result["completionAccounted"] is True


@pytest.mark.asyncio
async def test_a_refused_create_hands_the_fire_back_for_the_next_tick(seeded) -> None:
    """Provably-no-row is retryable, and the retry actually resolves it.

    The refusal comes from the real boundary tool rejecting a real
    unsupported combination -- nothing is substituted.
    """
    event_uuid, symbol, _ = FIRES[0]

    store = DatabaseClaimStore(session_factory=seeded)
    refusing = spawner_for({event_uuid: unsupported_draft(event_uuid, symbol)})
    first = await run_watch_repricing_tick(store=store, spawner=refusing, now=TICK)

    assert await proposal_count(seeded) == 0
    assert first["completionComplete"] is False
    first_rows = {row["eventUuid"]: row for row in first["completion"]}
    assert first_rows[event_uuid]["deferralReason"] == "spawn_not_started"

    # Next tick, with the real tool: the same fire resolves.
    second = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded),
        spawner=spawner_for(
            {event_uuid: sell_draft(event_uuid, symbol, limit_price="286000")}
        ),
        now=TICK + dt.timedelta(minutes=1),
    )
    second_rows = {row["eventUuid"]: row for row in second["completion"]}
    assert second_rows[event_uuid]["state"] == ClaimLifecycle.PROPOSAL_CREATED
    assert second_rows[event_uuid]["proposalId"] in await proposal_ids(seeded)


@contextlib.contextmanager
def lost_acknowledgement():
    """Make the boundary raise, as a lost acknowledgement would.

    Patches the module global rather than passing a substitute, because
    there is no parameter to pass one to (r2 / BLOCKER 1). This is a
    test-runtime patch, not a configuration path.

    Deliberately its own :meth:`pytest.MonkeyPatch.context` rather than the
    test's ``monkeypatch`` fixture: ``monkeypatch.undo()`` reverts *every*
    patch made through that fixture, including the ``enabled`` fixture's,
    which silently turned the tick off and made the assertions afterwards
    vacuous.
    """
    from app.services.watch_trigger_repricing import proposal_chain

    async def boom(**kwargs):
        raise TimeoutError("acknowledgement lost after commit")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proposal_chain, "order_proposal_create", boom)
        yield


@pytest.mark.asyncio
async def test_an_ambiguous_create_is_quarantined_and_never_double_proposes(
    seeded,
) -> None:
    event_uuid, symbol, _ = FIRES[0]
    draft = sell_draft(event_uuid, symbol, limit_price="286000")

    store = DatabaseClaimStore(session_factory=seeded)
    with lost_acknowledgement():
        first = await run_watch_repricing_tick(
            store=store, spawner=spawner_for({event_uuid: draft}), now=TICK
        )
    assert [row["eventUuid"] for row in first["needsReconcile"]] == [event_uuid]
    assert first["completionQuarantined"] == [event_uuid]

    rows = {row["eventUuid"]: row for row in first["completion"]}
    # Reported as terminal-unknown, not as "deferred to a later tick".
    assert rows[event_uuid]["state"] == ClaimLifecycle.AWAITING_RECONCILE
    assert not rows[event_uuid]["proposalId"]
    assert not rows[event_uuid]["rejectionReason"]
    assert first["completionComplete"] is False
    assert first["completionAccounted"] is True
    assert event_uuid not in first["completionDeferred"]

    # The refusal is a row, not a log line.
    async with seeded() as session:
        state = await session.scalar(
            select(WatchEventRepricingClaim.state).where(
                WatchEventRepricingClaim.event_uuid == uuid.UUID(event_uuid)
            )
        )
    assert state == ClaimLifecycle.AWAITING_RECONCILE

    # A later tick, with the real boundary, must not re-judge it.
    second = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded),
        spawner=spawner_for({event_uuid: draft}),
        now=TICK + dt.timedelta(minutes=1),
    )
    assert event_uuid not in {row["eventUuid"] for row in second["spawned"]}
    assert await proposal_count(seeded) == 0


@pytest.mark.asyncio
async def test_the_ttl_cannot_walk_a_quarantine_back_into_a_second_proposal(
    seeded,
) -> None:
    """r2 / BLOCKER 2, end to end.

    r1 left an ambiguous fire's claim in ``started`` and said in a log
    line that it would not be retried. ``started`` is the state the lease
    expires, so once the 30-minute lease ran out the TTL wrote
    ``expired_unprocessed``, the next tick re-claimed at generation + 1,
    and the fire was judged again -- creating a second proposal if the
    first call had committed and only its acknowledgement was lost.

    This drives the clock well past the lease and asserts the fire is
    still not re-judged and still produces no proposal.
    """
    event_uuid, symbol, _ = FIRES[0]
    draft = sell_draft(event_uuid, symbol, limit_price="286000")

    with lost_acknowledgement():
        await run_watch_repricing_tick(
            store=DatabaseClaimStore(session_factory=seeded),
            spawner=spawner_for({event_uuid: draft}),
            now=TICK,
        )

    store = DatabaseClaimStore(session_factory=seeded)
    # Past the lease, by a wide margin, and sweep as the TTL path would.
    long_after = TICK + DEFAULT_LEASE + dt.timedelta(hours=2)
    assert await store.sweep_expired(now=long_after) == [], (
        "a terminal claim must not be swept; only 'started' leases expire"
    )

    # The claim store still refuses to hand it out ...
    assert await store.state_for(event_uuid, now=long_after) is (
        ConsumptionState.QUARANTINED
    )
    assert (
        await store.try_claim(
            event_uuid=event_uuid,
            symbol=symbol,
            market="kr",
            claimed_by="later-tick",
            now=long_after,
        )
        is None
    )

    # ... and a full tick long after the lease creates no second proposal.
    later = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded),
        spawner=spawner_for({event_uuid: draft}),
        now=dt.datetime(2026, 8, 18, 14, 30, tzinfo=KST),
    )
    assert event_uuid not in {row["eventUuid"] for row in later["spawned"]}
    assert await proposal_count(seeded) == 0

    skipped = {row["eventUuid"]: row["reason"] for row in later["skipped"]}
    assert skipped[event_uuid] == "awaiting_spawn_reconcile"


@pytest.mark.asyncio
async def test_a_quarantine_does_not_block_other_fires_on_the_same_symbol(
    seeded,
) -> None:
    """Quarantine is per-event. The symbol slot is freed, not held forever."""
    quarantined_uuid, symbol, _ = FIRES[0]
    with lost_acknowledgement():
        await run_watch_repricing_tick(
            store=DatabaseClaimStore(session_factory=seeded),
            spawner=spawner_for(
                {
                    quarantined_uuid: sell_draft(
                        quarantined_uuid, symbol, limit_price="286000"
                    )
                }
            ),
            now=TICK,
        )

    store = DatabaseClaimStore(session_factory=seeded)
    assert await store.active_symbols(now=TICK + dt.timedelta(minutes=1)) == frozenset()

    other = BY_SYMBOL["000660"]
    result = await run_watch_repricing_tick(
        store=store,
        spawner=spawner_for({other: sell_draft(other, "000660", limit_price="205000")}),
        now=TICK + dt.timedelta(minutes=1),
    )
    rows = {row["eventUuid"]: row for row in result["completion"]}
    assert rows[other]["state"] == ClaimLifecycle.PROPOSAL_CREATED
    assert await proposal_count(seeded) == 1


# ---------------------------------------------------------------------------
# NO_BYPASS / NOT_ARMED, against the shipping entrypoint
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_proposal_creating_spawner_is_blocked_on_a_volatile_store(
    seeded,
) -> None:
    from app.services.watch_trigger_repricing.claims import InMemoryClaimStore

    event_uuid, symbol, _ = FIRES[0]
    result = await run_watch_repricing_tick(
        store=InMemoryClaimStore(),
        spawner=spawner_for(
            {event_uuid: sell_draft(event_uuid, symbol, limit_price="286000")}
        ),
        now=TICK,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "non_durable_claim_store"
    assert await proposal_count(seeded) == 0


@pytest.mark.asyncio
async def test_the_default_entrypoint_still_creates_nothing(seeded) -> None:
    """NOT_ARMED: with no spawner injected the tick is the dry rehearsal."""
    result = await run_watch_repricing_tick(
        store=DatabaseClaimStore(session_factory=seeded), now=TICK
    )

    assert result["status"] == "ok"
    assert await proposal_count(seeded) == 0
    assert result["completionComplete"] is False
