"""ROB-1286 B3 — DB poll -> spawn -> order_proposal_create, end to end.

r2's finding: the previous "E2E" hand-assembled a fake source, called the
poller itself, and passed the result into the tick, so the wiring a real
deployment depends on -- *which rows the entrypoint sees* -- was never
exercised. The shipped shell, run with no arguments, polled nothing.

This test calls :func:`run_watch_repricing_tick` with **no source
injected**, so it builds ``DatabaseWatchEventSource`` over the app's real
session factory and reads rows this test seeded into
``review.investment_watch_events`` in the run-owned test database. The
proposal at the far end is a real ``review.order_proposals`` row created by
the real ``order_proposal_create`` tool, and the test asserts against that
row's id.

What is still a stand-in, precisely: the *session* itself. No Claude
process is started; ``ScriptedDrySessionSpawner`` calls
``order_proposal_create`` in-process where a live session would call it
over MCP. Everything on either side of that -- poll, gate, selection,
claim, fencing, terminal write, completion mapping -- is the shipping code
against the real database.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.mcp_server.caller_identity import caller_agent_id_var
from app.models.investment_reports import InvestmentWatchEvent
from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalDispatchAttempt,
    OrderProposalRung,
)
from app.models.watch_event_repricing_claims import WatchEventRepricingClaim
from app.services.watch_trigger_repricing.db_claim_store import DatabaseClaimStore
from app.services.watch_trigger_repricing.entrypoint import run_watch_repricing_tick
from app.services.watch_trigger_repricing.lifecycle import (
    ClaimLifecycle,
    proposal_created,
    rejected,
)
from app.services.watch_trigger_repricing.spawn import ScriptedDrySessionSpawner

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("enabled")]

KST = dt.timezone(dt.timedelta(hours=9))
FIRE = dt.datetime(2026, 8, 18, 9, 5, tzinfo=KST)
TICK = dt.datetime(2026, 8, 18, 9, 6, tzinfo=KST)

RUNG_1_UUID = str(uuid.UUID(int=101286001))
RUNG_2_UUID = str(uuid.UUID(int=101286002))


@pytest.fixture(autouse=True)
def _caller_identity():
    token = caller_agent_id_var.set("rob1286-e2e")
    try:
        yield
    finally:
        caller_agent_id_var.reset(token)


async def _seed_fires(session) -> None:
    """The real 08-18 Samsung ladder: two rungs, both delivered, both unjudged."""
    for index, (event_uuid, threshold) in enumerate(
        ((RUNG_1_UUID, 276000), (RUNG_2_UUID, 282000)), start=1
    ):
        session.add(
            InvestmentWatchEvent(
                event_uuid=uuid.UUID(event_uuid),
                idempotency_key=f"rob1286-e2e:{event_uuid}",
                market="kr",
                target_kind="asset",
                symbol="005930",
                metric="price",
                operator="above",
                threshold=threshold,
                threshold_key=f"price_above_{threshold}",
                intent="sell_review",
                action_mode="approval_required",
                outcome="review_required",
                correlation_id=f"rob1286-e2e-{index}",
                kst_date="2026-08-18",
                delivery_status="delivered",
                delivered_at=FIRE,
            )
        )
    await session.commit()


@pytest_asyncio.fixture
async def seeded(_bootstrap_test_schema):
    from app.core.db import AsyncSessionLocal

    async def _clean() -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(WatchEventRepricingClaim))
            await session.execute(
                delete(InvestmentWatchEvent).where(
                    InvestmentWatchEvent.correlation_id.like("rob1286-e2e%")
                )
            )
            # Children first: proposals gain dispatch-attempt and rung rows.
            mine = select(OrderProposal.id).where(
                OrderProposal.proposer == "rob1286-e2e"
            )
            await session.execute(
                delete(OrderProposalApprovalDispatchAttempt).where(
                    OrderProposalApprovalDispatchAttempt.proposal_pk.in_(mine)
                )
            )
            await session.execute(
                delete(OrderProposalRung).where(OrderProposalRung.proposal_pk.in_(mine))
            )
            await session.execute(
                delete(OrderProposal).where(OrderProposal.proposer == "rob1286-e2e")
            )
            await session.commit()

    await _clean()
    async with AsyncSessionLocal() as session:
        await _seed_fires(session)
    yield AsyncSessionLocal
    await _clean()


async def _create_real_proposal() -> str:
    """Call the real tool, exactly as a spawned session would."""
    from app.mcp_server.tooling import order_proposal_tools as opt

    created = await opt.order_proposal_create(
        symbol="005930",
        market="equity_kr",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="rob1286-e2e",
        thesis="watch rung 276,000 crossed; trim into resistance",
        strategy="watch-trigger-repricing",
        rungs=[
            {
                "rung_index": 0,
                "side": "sell",
                "quantity": "10",
                "limit_price": "286000",
                "notional": None,
            }
        ],
    )
    assert created["success"] is True, created
    return str(created["proposal_id"])


@pytest.mark.asyncio
async def test_entrypoint_polls_the_database_itself(seeded) -> None:
    """No source injected: the tick must find the rows on its own."""
    store = DatabaseClaimStore(session_factory=seeded)

    result = await run_watch_repricing_tick(
        store=store, spawner=ScriptedDrySessionSpawner(), now=TICK
    )

    assert result["status"] == "ok"
    polled = {row["eventUuid"] for row in result["polled"]}
    assert polled == {RUNG_1_UUID, RUNG_2_UUID}


@pytest.mark.asyncio
async def test_full_chain_reaches_a_real_proposal_row(seeded) -> None:
    store = DatabaseClaimStore(session_factory=seeded)
    proposal_id = await _create_real_proposal()

    spawner = ScriptedDrySessionSpawner(
        scripted={
            RUNG_1_UUID: proposal_created(proposal_id),
            RUNG_2_UUID: rejected(
                "rung 2 not crossed at judge time; one ladder step is enough"
            ),
        }
    )

    result = await run_watch_repricing_tick(store=store, spawner=spawner, now=TICK)

    # The proposal id in the mapping is a row that exists.
    async with seeded() as session:
        row = await session.scalar(
            select(OrderProposal).where(
                OrderProposal.proposal_id == uuid.UUID(proposal_id)
            )
        )
    assert row is not None
    assert row.symbol == "005930"
    assert row.side == "sell"

    mapping = {r["eventUuid"]: r for r in result["completion"]}
    assert mapping[RUNG_1_UUID]["proposalId"] == proposal_id
    assert mapping[RUNG_1_UUID]["state"] == ClaimLifecycle.PROPOSAL_CREATED


@pytest.mark.asyncio
async def test_deferral_is_named_and_never_counted_as_complete(seeded) -> None:
    """Two rungs on one symbol: the second is deferred, not judged.

    Per-symbol concurrency means one tick can only judge one of them. That
    is correct -- two sessions sizing a sell against the same position is
    exactly what the rule prevents -- but the deferred fire must be
    *reported* as deferred and must **not** make the run look complete.
    """
    store = DatabaseClaimStore(session_factory=seeded)
    proposal_id = await _create_real_proposal()

    spawner = ScriptedDrySessionSpawner(
        scripted={RUNG_1_UUID: proposal_created(proposal_id)}
    )
    result = await run_watch_repricing_tick(store=store, spawner=spawner, now=TICK)

    mapping = {r["eventUuid"]: r for r in result["completion"]}
    assert set(mapping) == {RUNG_1_UUID, RUNG_2_UUID}
    assert mapping[RUNG_2_UUID]["state"] == "deferred"
    assert mapping[RUNG_2_UUID]["deferralReason"] == "symbol_already_in_flight"

    # Nothing vanished ...
    assert result["completionAccounted"] is True
    # ... but a deferred fire is not a judged fire.
    assert result["completionComplete"] is False


@pytest.mark.asyncio
async def test_the_loop_converges_every_fire_resolves_across_ticks(seeded) -> None:
    """The real completion bar: N fires in, N outcomes out.

    Deferral only stops being a failure if a later tick actually resolves
    the fire, so this drives ticks until the mapping is complete instead of
    accepting "deferred" as an answer.
    """
    store = DatabaseClaimStore(session_factory=seeded)
    resolved: dict[str, dict] = {}

    for index in range(4):
        proposal_id = await _create_real_proposal()
        pending = [u for u in (RUNG_1_UUID, RUNG_2_UUID) if u not in resolved]
        if not pending:
            break
        # Whatever this tick manages to start, it judges.
        spawner = ScriptedDrySessionSpawner(
            scripted={
                pending[0]: proposal_created(proposal_id)
                if index == 0
                else rejected("first rung already covers the ladder step")
            }
        )
        result = await run_watch_repricing_tick(
            store=store, spawner=spawner, now=TICK + dt.timedelta(minutes=index)
        )
        for row in result["completion"]:
            if row["state"] in {"proposal_created", "rejected_with_reason"}:
                resolved[row["eventUuid"]] = row

    assert set(resolved) == {RUNG_1_UUID, RUNG_2_UUID}, (
        "every polled fire must end as a proposal or an attributed reason"
    )
    assert resolved[RUNG_1_UUID]["proposalId"]
    assert resolved[RUNG_2_UUID]["rejectionReason"]


@pytest.mark.asyncio
async def test_an_analysis_only_run_fails_the_completion_criterion(seeded) -> None:
    """A session that judged nothing must not read as a successful run."""
    store = DatabaseClaimStore(session_factory=seeded)

    result = await run_watch_repricing_tick(
        store=store, spawner=ScriptedDrySessionSpawner(), now=TICK
    )

    assert result["completionComplete"] is False
    unmapped = [r for r in result["completion"] if r["state"] == "unmapped"]
    assert unmapped, "an unjudged fire must appear as unmapped, not be omitted"


@pytest.mark.asyncio
async def test_terminals_are_persisted_through_the_fenced_handle(seeded) -> None:
    store = DatabaseClaimStore(session_factory=seeded)
    proposal_id = await _create_real_proposal()

    spawner = ScriptedDrySessionSpawner(
        scripted={RUNG_1_UUID: proposal_created(proposal_id)}
    )
    await run_watch_repricing_tick(store=store, spawner=spawner, now=TICK)

    outcomes = await store.outcomes_for([RUNG_1_UUID])
    assert outcomes[RUNG_1_UUID].state is ClaimLifecycle.PROPOSAL_CREATED
    assert outcomes[RUNG_1_UUID].proposal_id == proposal_id


@pytest.mark.asyncio
async def test_second_tick_does_not_re_judge_a_resolved_fire(seeded) -> None:
    """Cross-run dedup, against the database rather than a process singleton."""
    store = DatabaseClaimStore(session_factory=seeded)
    proposal_id = await _create_real_proposal()

    first = await run_watch_repricing_tick(
        store=store,
        spawner=ScriptedDrySessionSpawner(
            scripted={RUNG_1_UUID: proposal_created(proposal_id)}
        ),
        now=TICK,
    )
    assert len(first["spawned"]) == 1

    # A *fresh* store object, as a separate flow run would have.
    second_store = DatabaseClaimStore(session_factory=seeded)
    second = await run_watch_repricing_tick(
        store=second_store,
        spawner=ScriptedDrySessionSpawner(),
        now=TICK + dt.timedelta(minutes=1),
    )

    resolved_uuid = first["spawned"][0]["eventUuid"]
    respawned = {row["eventUuid"] for row in second["spawned"]}
    assert resolved_uuid not in respawned
