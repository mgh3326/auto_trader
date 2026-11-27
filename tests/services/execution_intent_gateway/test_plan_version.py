"""hk 6991 — v3 intent supersession replay cases (design 6939 §5.2: R1, R1b, R1c, R2).

Fake clock, fake broker spy. The real :class:`IntentGateway`, the real
:class:`KisMockForwarder` submit/cancel (with the broker functions replaced by a
spy), the real issuer (:func:`plan_issuer.build_intent`) and the real v3 rules
(:mod:`plan_binding`, :mod:`daily_plan.row_lock`) run; only the stores are in
memory. The per-row advisory lock is an :class:`asyncio.Lock` per (account,
symbol, venue) taken exactly where the SQL guard takes it: by publish for the
rows it changes, by the reservation, and by the pre-send hook through the send.
The DB guard and trigger run in CI (``test_plan_version_db.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.schemas.execution_intent import CancelChild, SubmitChild
from app.services.daily_plan.row_lock import (
    changes_execution,
    latest_change_seq,
    lock_identity,
)
from app.services.daily_plan.validator import row_key
from app.services.execution_intent_gateway import kis_mock_forwarder as forwarder_module
from app.services.execution_intent_gateway import plan_binding
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.kis_mock_forwarder import KisMockForwarder
from app.services.execution_intent_gateway.plan_issuer import (
    IssueRequest,
    build_intent,
)
from app.services.execution_intent_gateway.store import Decide, Reservation
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    NOW,
    FakeKillSwitch,
    MemoryGatewayStore,
)

ACCOUNT = "acct_" + "a" * 32
SYMBOL = "005930"
KEY = ("candidate", "kr", SYMBOL, None)
HEAD = uuid4()


def _row(**overrides: Any) -> dict[str, Any]:
    body = {
        "row_kind": "candidate",
        "market": "kr",
        "symbol": SYMBOL,
        "account_ref": None,
        "would_be_account_ref": ACCOUNT,
        "would_be_account_mode": "kis_mock",
        "action": "new",
        "venue": "KRX",
        "session": "regular",
        "side": "buy",
        "valid_until": (NOW + timedelta(hours=6)).isoformat(),
        "trigger": {"kind": "price_at_or_below", "value": "70000"},
        "invalidation_conditions": [{"kind": "price_at_or_below", "value": "65000"}],
        "max_buy_price": "70500",
        "would_be_limit_price": "70000",
        "would_be_quantity": "10",
        "disposition": "adopted",
        "not_executable_reason": None,
    }
    body.update(overrides)
    return body


class MemoryPlan:
    """daily_plan publish and fold, with the per-row lock."""

    def __init__(self) -> None:
        self.seq = 0
        self.history: dict[tuple, list[tuple[int, str, dict]]] = defaultdict(list)
        self.current: dict[tuple, dict] = {}
        self.locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def publish(self, kind: str, bodies: list[dict]) -> int:
        identities = set()
        for body in bodies:
            previous = self.current.get(row_key(body))
            if changes_execution(previous, body) and kind != "fill":
                for item in (previous, body):
                    if item and lock_identity(item):
                        identities.add(lock_identity(item))
        async with contextlib.AsyncExitStack() as stack:
            for identity in sorted(identities):
                await stack.enter_async_context(self.locks[identity])
            self.seq += 1
            for body in bodies:
                self.history[row_key(body)].append((self.seq, kind, body))
                self.current[row_key(body)] = body
            return self.seq

    def issue(self, *, seq: int | None = None, prior_filled: int = 0) -> Any:
        seq = self.seq if seq is None else seq
        body = next(b for s, _, b in self.history[KEY] if s <= seq)
        for s, _, b in self.history[KEY]:
            if s <= seq:
                body = b
        version = {
            "head_id": HEAD,
            "seq": seq,
            "strategy_id": "plan-entry-test",
            "policy_version": "policy-v1",
        }
        row = {"body": body, "action": body["action"], "symbol": body["symbol"]}
        return build_intent(
            version=version,
            row=row,
            request=IssueRequest(
                market="kr",
                day=NOW.date(),
                seq=seq,
                row_key=KEY,
                position=0,
                prior_filled_quantity=prior_filled,
            ),
            now=NOW,
        )


class MemoryFacts:
    def __init__(self, plan: MemoryPlan, store: V3MemoryStore) -> None:
        self.plan = plan
        self.store = store
        #: order root -> ledger proves finished (the kis_mock ledger reader's answer).
        self.terminal: dict[str, bool] = {}

    async def latest_change_seq(self, session: Any, intent: IntentSnapshot) -> int:
        return latest_change_seq(self.plan.history[plan_binding.row_key_of(intent)])

    async def prior_unsettled(self, session: Any, intent: IntentSnapshot) -> bool:
        for other in self.store.intents.values():
            if (
                other.intent_id == intent.intent_id
                or other.schema_version != 3
                or (other.plan_account_ref, other.symbol, other.plan_venue)
                != (intent.plan_account_ref, intent.symbol, intent.plan_venue)
            ):
                continue
            if plan_binding.intent_unsettled(
                self.store._of(other.intent_id), self.terminal
            ):
                return True
        return False


class _NoSession:
    async def execute(self, *_: Any) -> None:
        return None


class MemoryPlanGuard:
    def __init__(self, plan: MemoryPlan, facts: MemoryFacts) -> None:
        self.plan = plan
        self.facts = facts
        self.before_pre_send: Any = None

    def _lock(self, intent: IntentSnapshot) -> asyncio.Lock:
        return self.plan.locks[
            (intent.plan_account_ref, intent.symbol, intent.plan_venue)
        ]

    async def at_reservation(
        self, session: Any, intent: IntentSnapshot, action: str
    ) -> Refusal | None:
        if action == "cancel":
            return None
        async with self._lock(intent):
            return await plan_binding.decide_under_lock(
                _NoSession(), self.facts, intent, action
            )

    @contextlib.asynccontextmanager
    async def pre_send(
        self, intent: IntentSnapshot, action: str
    ) -> AsyncIterator[Refusal | None]:
        if self.before_pre_send is not None:
            await self.before_pre_send()
        async with self._lock(intent):
            yield await plan_binding.decide_under_lock(
                _NoSession(), self.facts, intent, action
            )


class V3MemoryStore(MemoryGatewayStore):
    """``MemoryGatewayStore`` plus the v3 reservation check and ``not_sent``,
    in the order ``SqlAlchemyGatewayStore.reserve`` applies them.
    """

    guard: MemoryPlanGuard

    async def reserve(
        self, request: Any, *, request_digest: str, decide: Decide, now: Any
    ) -> Reservation:
        intent = self.intents.get(request.intent_id)
        known = any(c.idempotency_key == request.idempotency_key for c in self.children)
        if intent is None or known or intent.schema_version != 3:
            return await super().reserve(
                request, request_digest=request_digest, decide=decide, now=now
            )
        refusal = await self.guard.at_reservation(None, intent, request.action)

        def plan_decide(snapshot: IntentSnapshot, children: Any) -> Any:
            from app.services.execution_intent_gateway.decisions import Decision

            return Decision(refusal=refusal) if refusal else decide(snapshot, children)

        return await super().reserve(
            request, request_digest=request_digest, decide=plan_decide, now=now
        )

    async def finish(self, child_id: int, *, state: ChildState, **kwargs: Any) -> bool:
        reason = kwargs.pop("refusal_reason", None)
        recorded = await super().finish(child_id, state=state, **kwargs)
        if recorded and reason is not None:
            index = next(
                i for i, c in enumerate(self.children) if c.child_id == child_id
            )
            self.children[index] = replace(self.children[index], refusal_reason=reason)
        return recorded


class BrokerSpy:
    """Replaces the KIS mock order functions the forwarder calls."""

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.on_send: Any = None
        self._order = 9000

    async def place(self, **kwargs: Any) -> dict[str, Any]:
        self.sends.append(f"place:{kwargs['correlation_id']}")
        if self.on_send is not None:
            await self.on_send()
        self._order += 1
        return {"success": True, "order_no": str(self._order)}

    async def cancel(self, **kwargs: Any) -> dict[str, Any]:
        self.sends.append(f"cancel:{kwargs['order_id']}")
        return {"success": True, "broker_cancel_confirmed": True}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    import app.mcp_server.tooling.order_execution as order_execution
    import app.mcp_server.tooling.orders_modify_cancel as modify_cancel

    spy = BrokerSpy()
    monkeypatch.setattr(order_execution, "_place_order_impl", spy.place)
    monkeypatch.setattr(modify_cancel, "cancel_order_impl", spy.cancel)

    async def direct(self: Any, intent: Any, *, send: Any, **_: Any) -> Any:
        # Capability minting for follow-ups has its own tests
        # (test_followup_capability.py); here the cancel goes straight through.
        return await send()

    monkeypatch.setattr(forwarder_module.KisMockForwarder, "_with_capability", direct)

    plan = MemoryPlan()
    store = V3MemoryStore()
    facts = MemoryFacts(plan, store)
    guard = MemoryPlanGuard(plan, facts)
    store.guard = guard
    gateway = IntentGateway(
        store=store,
        forwarders={"kis_mock": KisMockForwarder(journal=store)},
        kill_switch=FakeKillSwitch(),
        environment=ENABLED,
        clock=lambda: NOW,
        plan_guard=guard,
    )

    class World:
        pass

    w = World()
    w.spy, w.plan, w.store, w.facts, w.guard, w.gateway = (
        spy,
        plan,
        store,
        facts,
        guard,
        gateway,
    )

    def add(intent: Any) -> IntentSnapshot:
        snapshot = plan_binding.snapshot_of_v3(intent)
        store.intents[snapshot.intent_id] = snapshot
        return snapshot

    w.add = add
    return w


def _submit(intent_id: UUID, key: str, quantity: int = 10) -> SubmitChild:
    return SubmitChild(
        action="submit",
        intent_id=intent_id,
        idempotency_key=key,
        quantity=quantity,
        limit_price_krw=70_000,
    )


def _states(store: V3MemoryStore) -> dict[str, tuple[str, str | None]]:
    return {
        c.idempotency_key: (c.state.value, c.refusal_reason) for c in store.children
    }


@pytest.mark.asyncio
async def test_r1_send_just_before_the_version_switch(world: Any) -> None:
    await world.plan.publish("premarket", [_row()])
    v1 = world.add(world.plan.issue())

    first = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )
    assert first.status == "accepted" and len(world.spy.sends) == 1

    await world.plan.publish("event", [_row(would_be_limit_price="69500")])

    retry = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )
    assert retry.status == "accepted" and retry.replayed
    stale = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0002", quantity=1), confirm=True
    )
    assert (stale.status, stale.reason) == ("refused", "plan_version_stale")

    v2 = world.add(world.plan.issue())
    blocked = await world.gateway.handle(
        _submit(v2.intent_id, "v2-child-0001"), confirm=True
    )
    assert (blocked.status, blocked.reason) == ("refused", "prior_intent_unsettled")

    assert _states(world.store) == {
        "v1-child-0001": ("accepted", None),
        "v1-child-0002": ("refused", "plan_version_stale"),
        "v2-child-0001": ("refused", "prior_intent_unsettled"),
    }
    assert len(world.spy.sends) == 1


@pytest.mark.asyncio
async def test_r1b_publish_wins_the_lock_before_the_hook(world: Any) -> None:
    await world.plan.publish("premarket", [_row()])
    v1 = world.add(world.plan.issue())

    async def publish_v2() -> None:
        world.guard.before_pre_send = None
        await world.plan.publish("event", [_row(would_be_quantity="8")])

    world.guard.before_pre_send = publish_v2
    outcome = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )

    assert (outcome.status, outcome.reason) == ("refused", "plan_version_stale")
    assert _states(world.store) == {"v1-child-0001": ("not_sent", "plan_version_stale")}
    assert world.spy.sends == []
    replay = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )
    assert replay.status == "refused" and replay.replayed and world.spy.sends == []


@pytest.mark.asyncio
async def test_r1c_publish_during_the_held_hook_waits_and_commits_after_the_send(
    world: Any,
) -> None:
    await world.plan.publish("premarket", [_row()])
    v1 = world.add(world.plan.issue())
    publish_task: list[asyncio.Task[int]] = []

    async def start_publish() -> None:
        publish_task.append(
            asyncio.create_task(
                world.plan.publish("event", [_row(would_be_limit_price="69500")])
            )
        )
        await asyncio.sleep(0)  # the publish runs until it waits on the row lock
        assert world.plan.seq == 1 and not publish_task[0].done()

    world.spy.on_send = start_publish
    outcome = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )
    world.spy.on_send = None
    assert outcome.status == "accepted"
    assert await publish_task[0] == 2  # committed after the send

    after = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0002", quantity=1), confirm=True
    )
    assert (after.status, after.reason) == ("refused", "plan_version_stale")
    assert len(world.spy.sends) == 1


@pytest.mark.asyncio
async def test_r2_version_switch_after_a_partial_fill(world: Any) -> None:
    await world.plan.publish("premarket", [_row()])
    v1 = world.add(world.plan.issue())
    sent = await world.gateway.handle(
        _submit(v1.intent_id, "v1-child-0001"), confirm=True
    )
    assert sent.status == "accepted"
    # 4 of 10 fill (in-effect ledger); the order stays open with 6.
    filled = 4

    await world.plan.publish("event", [_row(would_be_quantity="8")])
    v2 = world.add(world.plan.issue(prior_filled=filled))
    # Budget is the v2 approval minus the fills, never reset by v2.
    assert v2.max_quantity == 8 - filled
    assert v2.max_notional_krw == (8 - filled) * 70_000

    early = await world.gateway.handle(
        _submit(v2.intent_id, "v2-child-0001", 4), confirm=True
    )
    assert (early.status, early.reason) == ("refused", "prior_intent_unsettled")

    cancel = await world.gateway.handle(
        CancelChild(
            action="cancel",
            intent_id=v1.intent_id,
            idempotency_key="v1-cancel-0001",
            target_idempotency_key="v1-child-0001",
        ),
        confirm=True,
    )
    assert cancel.status == "accepted"

    # Cancelled at the broker but the ledger has not reconciled the fills yet.
    still = await world.gateway.handle(
        _submit(v2.intent_id, "v2-child-0002", 4), confirm=True
    )
    assert (still.status, still.reason) == ("refused", "prior_intent_unsettled")

    world.facts.terminal["v1-child-0001"] = True  # cancel + fills reconciled
    allowed = await world.gateway.handle(
        _submit(v2.intent_id, "v2-child-0003", 4), confirm=True
    )
    assert allowed.status == "accepted"
    over = await world.gateway.handle(
        _submit(v2.intent_id, "v2-child-0004", 1), confirm=True
    )
    assert (over.status, over.reason) == ("refused", "quantity_cap_exceeded")

    assert [send.split(":")[0] for send in world.spy.sends] == [
        "place",
        "cancel",
        "place",
    ]


@pytest.mark.asyncio
async def test_a_fill_version_or_an_unrelated_row_leaves_the_intent_sendable(
    world: Any,
) -> None:
    other = _row(symbol="000660", would_be_limit_price="100000", max_buy_price="100000")
    await world.plan.publish("premarket", [_row(), other])
    v1 = world.add(world.plan.issue())
    first = await world.gateway.handle(
        _submit(v1.intent_id, "v1-entry-0001", 5), confirm=True
    )
    assert first.status == "accepted"

    await world.plan.publish("fill", [_row()])  # observation only
    await world.plan.publish(
        "event",
        [
            _row(
                symbol="000660",
                would_be_quantity="3",
                would_be_limit_price="100000",
                max_buy_price="100000",
            )
        ],
    )

    # A later child of the same intent's lineage (e.g. after an invalidation
    # break the executor resizes) is still sendable.
    later = await world.gateway.handle(
        _submit(v1.intent_id, "v1-entry-0002", 5), confirm=True
    )
    assert later.status == "accepted"
    assert len(world.spy.sends) == 2


@pytest.mark.asyncio
async def test_a_fill_version_may_not_change_execution_fields() -> None:
    from app.services.daily_plan.store import _lock_changed_rows

    with pytest.raises(ValueError, match="fill_version_changes_execution"):
        await _lock_changed_rows(
            _NoSession(), "fill", [_row(would_be_quantity="3")], {KEY: _row()}
        )


@pytest.mark.asyncio
async def test_publish_locks_only_the_rows_it_changes(monkeypatch: Any) -> None:
    from app.services.daily_plan import store as plan_store

    locked: list[tuple[str, str, str]] = []

    async def record(_db: Any, *identity: str) -> None:
        locked.append(identity)

    monkeypatch.setattr(plan_store, "lock_plan_row", record)
    unchanged = _row(symbol="000660")
    current = {KEY: _row(), row_key(unchanged): unchanged}
    await plan_store._lock_changed_rows(
        _NoSession(), "event", [_row(would_be_quantity="3"), unchanged], current
    )
    assert locked == [(ACCOUNT, SYMBOL, "KRX")]


def test_cancel_is_never_plan_checked() -> None:
    snapshot = plan_binding.snapshot_of_v3(_issue_v1())
    assert (
        plan_binding.plan_refusal(
            snapshot, action="cancel", latest_change_seq=9, prior_unsettled=True
        )
        is None
    )


def _issue_v1() -> Any:
    plan = MemoryPlan()
    plan.seq = 1
    plan.history[KEY].append((1, "premarket", _row()))
    return plan.issue()


def test_unsettled_prior_intent_rule() -> None:
    def child(
        key: str, state: ChildState, action: str = "submit", target: str | None = None
    ) -> ChildSnapshot:
        return ChildSnapshot(
            child_id=1,
            idempotency_key=key,
            request_digest="d",
            action=action,
            target_idempotency_key=target,
            quantity=None if action == "cancel" else 1,
            limit_price_krw=None if action == "cancel" else 1,
            state=state,
            refusal_reason=None,
            broker_order_id="1",
            result=None,
            created_at=NOW,
        )

    assert plan_binding.intent_unsettled(
        [child("a", ChildState.UNCERTAIN)], {"a": True}
    )
    assert plan_binding.intent_unsettled([child("a", ChildState.SENDING)], {})
    assert plan_binding.intent_unsettled([child("a", ChildState.ACCEPTED)], {})
    assert not plan_binding.intent_unsettled(
        [child("a", ChildState.ACCEPTED)], {"a": True}
    )
    assert not plan_binding.intent_unsettled([child("a", ChildState.REFUSED)], {})


class _Rows:
    def __init__(self, records: list[Any]) -> None:
        self.records = records

    def __iter__(self) -> Any:
        return iter(self.records)


@pytest.mark.asyncio
async def test_soft_cancelled_prior_order_is_not_settled() -> None:
    from types import SimpleNamespace

    corr = forwarder_module.order_correlation_id("root-0001")

    class Session:
        def __init__(self, detail: Any) -> None:
            self.detail = detail

        async def execute(self, _stmt: Any) -> _Rows:
            return _Rows([(corr, "cancelled", self.detail)])

    reader = plan_binding.KisMockLedgerSettlement()
    soft = {
        "broker_cancel_confirmed": False,
        "reason": "soft_cancel_broker_unsupported",
    }
    assert await reader.terminal(Session(soft), ["root-0001"]) == {"root-0001": False}
    assert await reader.terminal(Session(None), ["root-0001"]) == {"root-0001": False}
    confirmed = {"broker_cancel_confirmed": True}
    assert await reader.terminal(Session(confirmed), ["root-0001"]) == {
        "root-0001": True
    }
    assert plan_binding.ledger_row_settled("expired", None)
    assert not plan_binding.ledger_row_settled("fill", None)


@pytest.mark.asyncio
async def test_an_adoption_replay_that_restores_an_old_body_is_a_change() -> None:
    from types import SimpleNamespace as NS

    from app.services.daily_plan.store import row_change_seq

    a, b = _row(), _row(would_be_quantity="8")
    rid_a, rid_b = uuid4(), uuid4()
    v1, v2, v3 = uuid4(), uuid4(), uuid4()
    row_records = [
        NS(id=rid_a, version_id=v1, body=a),
        NS(id=rid_b, version_id=v2, body=b),
    ]
    version_records = [
        NS(id=v1, seq=1, kind="premarket", metadata={}),
        NS(id=v2, seq=2, kind="event", metadata={}),
        NS(id=v3, seq=3, kind="event", metadata={"adoption_replay_rows": [str(rid_a)]}),
    ]

    class Db:
        calls = 0

        async def execute(self, _stmt: Any) -> _Rows:
            Db.calls += 1
            return _Rows(row_records if Db.calls == 1 else version_records)

    assert await row_change_seq(Db(), head_id=HEAD, key=KEY) == 3
