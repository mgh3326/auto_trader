"""Shared fakes for the intent gateway tests.

``MemoryGatewayStore`` keeps the same reserve contract as the SQLAlchemy store,
including the journal write before the decision returns. ``test_store_db.py`` runs
the real store against the run-owned test database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.schemas.execution_intent import AmendChild, CancelChild, SubmitChild
from app.services.execution_intent_gateway.decisions import (
    Child,
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.gateway import ForwardResult
from app.services.execution_intent_gateway.store import (
    Decide,
    IntentJournal,
    Reservation,
)
from app.services.kis_mock_runner.control import KillMode, KillSwitchState

NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
ENABLED = {"EXECUTION_INTENT_GATEWAY_ENABLED": "true"}


def intent_snapshot(**overrides: Any) -> IntentSnapshot:
    values: dict[str, Any] = {
        "intent_id": uuid4(),
        "strategy": "s3-test",
        "account": "kis_mock",
        "venue": "krx",
        "symbol": "005930",
        "side": "buy",
        "max_quantity": 10,
        "max_notional_krw": 1_000_000,
        "limit_price_krw": 70_000,
        "max_slippage_bps": 100,
        "position_at_authorization": 0,
        "min_position_after": 0,
        "max_position_after": 10,
        "expires_at": NOW + timedelta(hours=1),
    }
    return IntentSnapshot(**{**values, **overrides})


class MemoryGatewayStore:
    """Every committed row is visible in ``children``; events record the order."""

    def __init__(self, *intents: IntentSnapshot, events: list[str] | None = None):
        self.intents: dict[UUID, IntentSnapshot] = {i.intent_id: i for i in intents}
        self.children: list[ChildSnapshot] = []
        self._intent_of_child: dict[int, UUID] = {}
        self.events = events if events is not None else []
        self.finish_error: Exception | None = None

    def child(self, key: str) -> ChildSnapshot:
        return next(c for c in self.children if c.idempotency_key == key)

    async def reserve(
        self, request: Child, *, request_digest: str, decide: Decide, now: datetime
    ) -> Reservation:
        existing = next(
            (c for c in self.children if c.idempotency_key == request.idempotency_key),
            None,
        )
        if existing is not None:
            if existing.request_digest != request_digest:
                return Reservation(conflict=True)
            return Reservation(child=existing)
        intent = self.intents.get(request.intent_id)
        if intent is None:
            return Reservation(refusal_without_row=Refusal.INTENT_NOT_FOUND)
        decision = decide(intent, self._of(request.intent_id))
        refused = decision.refusal is not None
        child = ChildSnapshot(
            child_id=len(self.children) + 1,
            idempotency_key=request.idempotency_key,
            request_digest=request_digest,
            action=request.action,
            target_idempotency_key=getattr(request, "target_idempotency_key", None),
            quantity=getattr(request, "quantity", None),
            limit_price_krw=getattr(request, "limit_price_krw", None),
            state=ChildState.REFUSED if refused else ChildState.SENDING,
            refusal_reason=decision.refusal.value if refused else None,
            broker_order_id=None,
            result=None,
        )
        self.children.append(child)
        self._intent_of_child[child.child_id] = request.intent_id
        self.events.append(f"journal:{child.state.value}:{request.idempotency_key}")
        return Reservation(intent=intent, child=child, is_new=True, decision=decision)

    async def read_intent_journal(self, intent_id: UUID) -> IntentJournal | None:
        intent = self.intents.get(intent_id)
        if intent is None:
            return None
        return IntentJournal(intent=intent, children=tuple(self._of(intent_id)))

    async def read_child(
        self, idempotency_key: str
    ) -> tuple[UUID, ChildSnapshot] | None:
        for child in self.children:
            if child.idempotency_key == idempotency_key:
                return self._intent_of_child[child.child_id], child
        return None

    def _of(self, intent_id: UUID) -> Sequence[ChildSnapshot]:
        return [
            child
            for child in self.children
            if self._intent_of_child[child.child_id] == intent_id
        ]

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
    ) -> bool:
        if self.finish_error is not None:
            raise self.finish_error
        for index, child in enumerate(self.children):
            if child.child_id == child_id and child.state is ChildState.SENDING:
                self.children[index] = ChildSnapshot(
                    child_id=child.child_id,
                    idempotency_key=child.idempotency_key,
                    request_digest=child.request_digest,
                    action=child.action,
                    target_idempotency_key=child.target_idempotency_key,
                    quantity=child.quantity,
                    limit_price_krw=child.limit_price_krw,
                    state=state,
                    refusal_reason=None,
                    broker_order_id=broker_order_id,
                    result=result,
                )
                self.events.append(f"finish:{state.value}:{child.idempotency_key}")
                return True
        return False


class FakeBroker:
    """Stands in for the broker service calls and counts every forward."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.events = events if events is not None else []
        self.raises: BaseException | None = None
        self.accepted = True
        self._next_order = 1000

    def _record(self, action: str, key: str) -> None:
        self.calls.append((action, key))
        self.events.append(f"forward:{action}:{key}")
        if self.raises is not None:
            raise self.raises

    def _order_id(self) -> str:
        self._next_order += 1
        return str(self._next_order)

    async def submit(
        self, intent: IntentSnapshot, request: SubmitChild
    ) -> ForwardResult:
        self._record("submit", request.idempotency_key)
        return ForwardResult(
            self.accepted, self._order_id(), {"success": self.accepted}
        )

    async def amend(
        self, intent: IntentSnapshot, request: AmendChild, *, broker_order_id: str
    ) -> ForwardResult:
        self._record("amend", broker_order_id)
        return ForwardResult(
            self.accepted, self._order_id(), {"success": self.accepted}
        )

    async def cancel(
        self, intent: IntentSnapshot, request: CancelChild, *, broker_order_id: str
    ) -> ForwardResult:
        self._record("cancel", broker_order_id)
        return ForwardResult(self.accepted, broker_order_id, {"success": self.accepted})


class FakeKillSwitch:
    def __init__(self, mode: KillMode = KillMode.ACTIVE) -> None:
        self.mode = mode
        self.read_error: Exception | None = None
        self.reads = 0

    async def read(self) -> KillSwitchState:
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return KillSwitchState(mode=self.mode, reason="test", updated_by="test")

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState:
        raise AssertionError("the gateway never writes the kill switch")
