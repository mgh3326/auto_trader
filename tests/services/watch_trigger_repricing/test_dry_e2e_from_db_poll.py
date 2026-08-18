"""ROB-1286 r2 / BLOCKER-3 — the dry E2E, starting at the DB poll.

r1's AC1 replay constructed ``CandidateEvent`` objects by hand, so it began
*after* the step a deployment can get wrong about which rows it sees, and it
never reached a proposal-create seam. This file runs the whole chain:

    DB read  ->  poll/map  ->  gate  ->  select  ->  claim  ->  spawn
             ->  the session's one permitted write

What is real here
-----------------
The repository method, the SQLAlchemy statement it builds (asserted on, so
the filters are proven, not assumed), the ORM model and its row shape, the
uuid -> str normalisation, poll-level dedup, the XKRX session gate, the
consumption criterion, claiming, per-symbol concurrency, the round cap and
overflow reporting.

What is a stand-in, and why
---------------------------
``AsyncSession``
    A recorder. It returns pre-built ORM instances and captures the
    statement. It cannot write: it has no ``add``/``commit``/``flush``, so
    an accidental write is an ``AttributeError``, not a row.
``SessionSpawner``
    :class:`_DrySessionRunner`. It does not start a session; it plays the
    part of one by calling a **stub** ``order_proposal_create`` so the
    chain terminates at the boundary seam rather than short of it.

So the residual is stated plainly: **no live database round-trip is
exercised** (it cannot be -- proving a real read needs real rows, and
writing rows to ``review.investment_watch_events`` is forbidden in this
round), **no session process is started, and no proposal row is created.**
The counters at the bottom assert those three zeros.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid

import pytest

from app.models.investment_reports import InvestmentWatchEvent
from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.consumption import ConsumptionState
from app.services.watch_trigger_repricing.event_source import DatabaseWatchEventSource
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.poller import (
    poll_candidate_events,
    to_candidate_event,
)
from app.services.watch_trigger_repricing.spawn import (
    EXECUTION_BOUNDARY,
    SpawnDisposition,
    SpawnOutcome,
)

from .conftest import INCIDENT_FIRE, INCIDENT_TICK

pytestmark = pytest.mark.unit

# The two 005930 rungs of the 08-18 incident, as rows.
RUNG_1_UUID = uuid.UUID("11111111-1111-4111-8111-111111111111")
RUNG_2_UUID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _row(
    *,
    event_uuid: uuid.UUID,
    symbol: str = "005930",
    market: str = "kr",
    outcome: str = "review_required",
    delivery_status: str = "delivered",
    delivered_at: dt.datetime | None = INCIDENT_FIRE,
) -> InvestmentWatchEvent:
    """A real ORM instance, never attached to a session."""
    return InvestmentWatchEvent(
        event_uuid=event_uuid,
        idempotency_key=f"idem-{event_uuid}",
        market=market,
        target_kind="asset",
        symbol=symbol,
        metric="price",
        operator="gte",
        threshold=276000,
        threshold_key=f"{symbol}-276000",
        intent="sell_review",
        action_mode="review",
        outcome=outcome,
        correlation_id=f"corr-{event_uuid}",
        kst_date="2026-08-18",
        delivery_status=delivery_status,
        delivered_at=delivered_at,
    )


class _Scalars:
    def __init__(self, rows: list[InvestmentWatchEvent]) -> None:
        self._rows = rows

    def all(self) -> list[InvestmentWatchEvent]:
        return list(self._rows)


class _RecordingSession:
    """Read-only stand-in for ``AsyncSession``.

    Deliberately implements ``scalars`` and nothing else: any write the
    code under test attempted would raise ``AttributeError`` rather than
    silently succeed.
    """

    def __init__(self, rows: list[InvestmentWatchEvent]) -> None:
        self._rows = rows
        self.statements: list[object] = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _Scalars(self._rows)


class _SessionFactory:
    def __init__(self, session: _RecordingSession) -> None:
        self.session = session
        self.opened = 0

    def __call__(self) -> _SessionFactory:
        self.opened += 1
        return self

    async def __aenter__(self) -> _RecordingSession:
        return self.session

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _ProposalCreateStub:
    """Stands in for the ``order_proposal_create`` MCP tool. Writes nothing."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, symbol: str, market: str, source_event_uuid: str) -> dict:
        self.calls.append(
            {
                "symbol": symbol,
                "market": market,
                "sourceEventUuid": source_event_uuid,
            }
        )
        return {"success": True, "proposalId": f"stub-{len(self.calls)}"}


class _DrySessionRunner:
    """Plays the part of the spawned session without starting one.

    Calls only tools its granted capability profile allows -- which is what
    makes reaching ``order_proposal_create`` here a boundary test and not a
    pretence.
    """

    is_dry = True

    def __init__(self, proposal_create: _ProposalCreateStub) -> None:
        self._proposal_create = proposal_create
        self.requests: list[object] = []

    def spawn(self, request) -> SpawnOutcome:
        self.requests.append(request)
        assert EXECUTION_BOUNDARY in request.capability_profile.tools
        self._proposal_create(
            symbol=request.symbol,
            market=request.market,
            source_event_uuid=request.event_uuid,
        )
        return SpawnOutcome(
            request=request,
            disposition=SpawnDisposition.DRY,
            detail="dry_run",
        )


async def _poll(rows: list[InvestmentWatchEvent], **kwargs):
    session = _RecordingSession(rows)
    factory = _SessionFactory(session)
    source = DatabaseWatchEventSource(factory)
    candidates = await poll_candidate_events(source, **kwargs)
    return candidates, session, factory


# ---------------------------------------------------------------------------
# 1. The poll itself is real
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_issues_a_filtered_read_and_writes_nothing() -> None:
    rows = [_row(event_uuid=RUNG_1_UUID)]

    candidates, session, factory = await _poll(
        rows, market="kr", delivered_since=INCIDENT_FIRE - dt.timedelta(minutes=5)
    )

    assert factory.opened == 1
    assert len(session.statements) == 1
    sql = str(session.statements[0])
    assert "investment_watch_events" in sql
    assert "ORDER BY" in sql

    # The predicates must be asserted on the WHERE clause specifically:
    # every column name also appears in the SELECT list, so searching the
    # whole statement would pass even with no filtering at all.
    where = sql.split(" \nWHERE ")[-1].split("ORDER BY")[0]
    assert "delivery_status" in where
    assert "delivered_at" in where
    assert "market" in where

    # A read, and only a read.
    for mutation in ("INSERT", "UPDATE", "DELETE"):
        assert mutation not in sql.upper()
    assert [c.event_uuid for c in candidates] == [str(RUNG_1_UUID)]


@pytest.mark.asyncio
async def test_poll_omits_predicates_it_was_not_given() -> None:
    """The WHERE assertions above only mean something if they can be absent."""
    _, session, _ = await _poll(
        [_row(event_uuid=RUNG_1_UUID)], market=None, delivered_since=None
    )

    sql = str(session.statements[0])
    where = sql.split(" \nWHERE ")[-1].split("ORDER BY")[0]
    assert "delivery_status" in where
    assert "delivered_at" not in where
    assert "market" not in where


@pytest.mark.asyncio
async def test_poll_normalises_the_uuid_to_the_claim_key_type() -> None:
    """A UUID key and its string form would be two different claims."""
    candidates, _, _ = await _poll([_row(event_uuid=RUNG_1_UUID)])

    assert isinstance(candidates[0].event_uuid, str)
    assert candidates[0].event_uuid == str(RUNG_1_UUID)


@pytest.mark.asyncio
async def test_poll_dedupes_an_overlapping_cursor() -> None:
    """Overlapping ``delivered_since`` windows must not eat a cap slot."""
    duplicated = [_row(event_uuid=RUNG_1_UUID), _row(event_uuid=RUNG_1_UUID)]

    candidates, _, _ = await _poll(duplicated)

    assert [c.event_uuid for c in candidates] == [str(RUNG_1_UUID)]


def test_row_mapping_preserves_every_field_selection_reads() -> None:
    row = _row(event_uuid=RUNG_2_UUID, symbol="039200", outcome="notified")
    candidate = to_candidate_event(row)

    assert candidate.symbol == "039200"
    assert candidate.market == "kr"
    assert candidate.outcome == "notified"
    assert candidate.delivery_status == "delivered"
    assert candidate.delivered_at == INCIDENT_FIRE


# ---------------------------------------------------------------------------
# 2. The full dry chain: poll -> ... -> proposal-create seam
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dry_e2e_from_db_poll_to_proposal_create() -> None:
    """AC1, end to end, with every stand-in named and counted."""
    rows = [_row(event_uuid=RUNG_1_UUID), _row(event_uuid=RUNG_2_UUID)]
    proposal_create = _ProposalCreateStub()
    runner = _DrySessionRunner(proposal_create)
    store = InMemoryClaimStore()

    candidates, session, _ = await _poll(rows)
    result = run_repricing_tick(
        candidates, store=store, now=INCIDENT_TICK, spawner=runner
    )

    # The chain ran, from the read to the boundary.
    assert len(session.statements) == 1
    assert result.gate.should_run is True
    assert len(result.spawned) == 1
    assert proposal_create.calls == [
        {
            "symbol": "005930",
            "market": "kr",
            "sourceEventUuid": str(RUNG_1_UUID),
        }
    ]

    # ...and the second rung is deferred, not dropped.
    deferred = [s for s in result.skipped if s.event_uuid == str(RUNG_2_UUID)]
    assert [s.reason for s in deferred] == ["symbol_already_in_flight"]

    # The three zeros.
    assert all(o.started is False for o in result.spawned), "a session was started"
    assert all(o.detail == "dry_run" for o in result.spawned)
    assert not hasattr(session, "commit"), "the stand-in session can write"
    assert result.needs_reconcile == ()


@pytest.mark.asyncio
async def test_dry_e2e_filters_non_review_rows_but_still_reports_them() -> None:
    """A polled row that is not a candidate must still be named in the report."""
    rows = [
        _row(event_uuid=RUNG_1_UUID, outcome="notified"),
        _row(event_uuid=RUNG_2_UUID, symbol="039200"),
    ]
    proposal_create = _ProposalCreateStub()

    candidates, _, _ = await _poll(rows)
    result = run_repricing_tick(
        candidates,
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK,
        spawner=_DrySessionRunner(proposal_create),
    )

    assert [c["symbol"] for c in proposal_create.calls] == ["039200"]
    reported = {s.event_uuid for s in result.skipped} | {
        o.request.event_uuid for o in result.spawned
    }
    assert reported == {str(RUNG_1_UUID), str(RUNG_2_UUID)}
    assert [s.reason for s in result.skipped if s.event_uuid == str(RUNG_1_UUID)] == [
        "outcome_not_consumable"
    ]


@pytest.mark.asyncio
async def test_dry_e2e_creates_exactly_one_proposal_across_two_polls() -> None:
    """The dedup that matters, measured where it matters: proposal count."""
    rows = [_row(event_uuid=RUNG_1_UUID)]
    proposal_create = _ProposalCreateStub()
    runner = _DrySessionRunner(proposal_create)
    store = InMemoryClaimStore()

    for offset in (0, 1):
        candidates, _, _ = await _poll(rows)
        run_repricing_tick(
            candidates,
            store=store,
            now=INCIDENT_TICK + dt.timedelta(minutes=offset),
            spawner=runner,
        )

    assert len(proposal_create.calls) == 1


# ---------------------------------------------------------------------------
# 3. The incident race, replayed against the polled rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_incident_race_replay_produces_exactly_one_proposal() -> None:
    """08-18: the 0905 rep session starts as the 09:05 fire lands.

    Both consumers go for the same polled row at the same instant, on real
    threads. Exactly one proposal may exist afterwards, and the fire must
    not end up owned by nobody -- which is what actually happened.
    """
    candidates, _, _ = await _poll([_row(event_uuid=RUNG_1_UUID)])
    store = InMemoryClaimStore()
    proposal_create = _ProposalCreateStub()
    runner = _DrySessionRunner(proposal_create)

    barrier = threading.Barrier(2)
    b_plan_won: list[bool] = []
    lock = threading.Lock()

    def b_plan_rep_session() -> None:
        """B안 claims through the same canonical criterion."""
        barrier.wait()
        claim = store.try_claim(
            event_uuid=str(RUNG_1_UUID),
            symbol="005930",
            claimed_by="kr-open-trade-0905",
            now=INCIDENT_TICK,
        )
        if claim is not None:
            store.mark_consumed(str(RUNG_1_UUID), reason="rep_session_handled")
            proposal_create(
                symbol="005930",
                market="kr",
                source_event_uuid=str(RUNG_1_UUID),
            )
        with lock:
            b_plan_won.append(claim is not None)

    def a_plan_tick() -> None:
        barrier.wait()
        run_repricing_tick(candidates, store=store, now=INCIDENT_TICK, spawner=runner)

    threads = [
        threading.Thread(target=b_plan_rep_session),
        threading.Thread(target=a_plan_tick),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    assert len(proposal_create.calls) == 1, (
        f"one fire produced {len(proposal_create.calls)} proposals"
    )
    assert store.state_for(str(RUNG_1_UUID), now=INCIDENT_TICK) is (
        ConsumptionState.CONSUMED
    )


@pytest.mark.asyncio
async def test_the_incident_outcome_itself_is_impossible_now() -> None:
    """The accident was zero proposals, not two. Guard that direction too."""
    candidates, _, _ = await _poll([_row(event_uuid=RUNG_1_UUID)])
    proposal_create = _ProposalCreateStub()

    run_repricing_tick(
        candidates,
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK,
        spawner=_DrySessionRunner(proposal_create),
    )

    assert len(proposal_create.calls) == 1, "the fire produced nothing, again"
