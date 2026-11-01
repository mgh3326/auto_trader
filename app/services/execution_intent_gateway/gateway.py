"""ROB-1440 — accept one child request against a stored intent and forward it.

The order of work is the contract:

1. Env gate and per-call confirm, before any DB access.
2. Kill state read (submit and amend only).
3. Reserve: under the intent row lock, replay a known key or decide, then commit
   the journal row. Nothing reaches a broker before that commit.
4. Forward through the existing broker service, once.
5. Record the outcome.

A process that dies after step 3 leaves the row ``sending``. A retry of that key
replays ``sending`` and never sends again. Only ledger evidence can tell what
happened, and the gateway does not read it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

from app.schemas.execution_intent import (
    AmendChild,
    SubmitChild,
    contract_digest,
)
from app.services.execution_intent_gateway.decisions import (
    MOCK_ACCOUNTS,
    Child,
    ChildSnapshot,
    ChildState,
    Decision,
    IntentSnapshot,
    Refusal,
    decide,
)
from app.services.execution_intent_gateway.store import GatewayStore
from app.services.kis_mock_runner.control import (
    KillMode,
    KillSwitchStore,
    read_effective_kill_switch,
)

logger = logging.getLogger(__name__)

GATEWAY_ENABLED_ENV: Final = "EXECUTION_INTENT_GATEWAY_ENABLED"
_MAX_RESULT_BYTES: Final = 16_384

OutcomeStatus = Literal["accepted", "uncertain", "sending", "refused", "conflict"]


@dataclass(frozen=True, slots=True)
class ForwardResult:
    """What a broker service call proved. Anything short of ``accepted`` is
    treated as an unknown outcome, because a failure result does not prove that
    the broker received nothing.
    """

    accepted: bool
    broker_order_id: str | None
    result: dict[str, Any]


class BrokerForwarder(Protocol):
    async def submit(
        self, intent: IntentSnapshot, request: SubmitChild
    ) -> ForwardResult: ...

    async def amend(
        self, intent: IntentSnapshot, request: AmendChild, *, broker_order_id: str
    ) -> ForwardResult: ...

    async def cancel(
        self, intent: IntentSnapshot, *, broker_order_id: str
    ) -> ForwardResult: ...


@dataclass(frozen=True, slots=True)
class GatewayOutcome:
    status: OutcomeStatus
    reason: str | None = None
    child_id: int | None = None
    broker_order_id: str | None = None
    result: dict[str, Any] | None = None
    replayed: bool = False
    outcome_not_recorded: bool = False


def _refused(reason: Refusal, child_id: int | None = None) -> GatewayOutcome:
    return GatewayOutcome(status="refused", reason=reason.value, child_id=child_id)


def _replay(child: ChildSnapshot) -> GatewayOutcome:
    return GatewayOutcome(
        status=child.state.value,
        reason=child.refusal_reason,
        child_id=child.child_id,
        broker_order_id=child.broker_order_id,
        result=child.result,
        replayed=True,
    )


def _bounded_result(raw: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy small enough to journal and replay."""
    try:
        encoded = json.dumps(raw, default=str, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"unserializable": True, "success": raw.get("success") is True}
    if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
        return {"truncated": True, "success": raw.get("success") is True}
    return json.loads(encoded)


class IntentGateway:
    def __init__(
        self,
        *,
        store: GatewayStore,
        forwarders: Mapping[str, BrokerForwarder],
        kill_switch: KillSwitchStore,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        not_mock = set(forwarders) - MOCK_ACCOUNTS
        if not_mock:
            raise ValueError(f"forwarders for non-mock accounts: {sorted(not_mock)}")
        self._store = store
        self._forwarders = dict(forwarders)
        self._kill_switch = kill_switch
        self._environment = os.environ if environment is None else environment
        self._clock = clock

    async def handle(self, request: Child, *, confirm: bool = False) -> GatewayOutcome:
        if self._environment.get(GATEWAY_ENABLED_ENV) != "true":
            return _refused(Refusal.GATEWAY_DISABLED)
        if confirm is not True:
            return _refused(Refusal.CONFIRM_REQUIRED)

        kill_mode: KillMode | None = None
        if request.action != "cancel":
            kill_mode = (await read_effective_kill_switch(self._kill_switch)).mode

        now = self._clock()
        reservation = await self._store.reserve(
            request,
            request_digest=contract_digest(request),
            now=now,
            decide=lambda intent, children: decide(
                request,
                intent=intent,
                children=children,
                # Read under the intent row lock: a wait for that lock must not
                # let an expired authorization pass on a stale time.
                now=self._clock(),
                kill_mode=kill_mode,
                forwarder_accounts=frozenset(self._forwarders),
            ),
        )

        if reservation.refusal_without_row is not None:
            return _refused(reservation.refusal_without_row)
        if reservation.conflict:
            return GatewayOutcome(status="conflict", reason="idempotency_key_reused")
        child = reservation.child
        if child is None:
            raise RuntimeError("gateway store returned no child row")
        if not reservation.is_new:
            return _replay(child)
        decision = reservation.decision
        if decision is None or reservation.intent is None:
            raise RuntimeError("gateway store returned an incomplete reservation")
        if decision.refusal is not None:
            return _refused(decision.refusal, child.child_id)

        return await self._forward_and_record(
            request, reservation.intent, child, decision
        )

    async def _forward_and_record(
        self,
        request: Child,
        intent: IntentSnapshot,
        child: ChildSnapshot,
        decision: Decision,
    ) -> GatewayOutcome:
        try:
            forwarded = await self._forward(request, intent, decision)
        except Exception as exc:  # noqa: BLE001 - the send may have crossed the wire
            logger.error(
                "intent gateway forward raised: child_id=%s error_class=%s",
                child.child_id,
                type(exc).__name__,
            )
            forwarded = ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"error_class": type(exc).__name__},
            )

        needs_order_id = request.action != "cancel"
        accepted = forwarded.accepted and (
            forwarded.broker_order_id is not None or not needs_order_id
        )
        state = ChildState.ACCEPTED if accepted else ChildState.UNCERTAIN
        broker_order_id = forwarded.broker_order_id
        result = _bounded_result(forwarded.result)

        try:
            recorded = await self._store.finish(
                child.child_id,
                state=state,
                broker_order_id=broker_order_id,
                result=result,
                now=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - the row stays sending, not resent
            logger.error(
                "intent gateway outcome not recorded: child_id=%s error_class=%s",
                child.child_id,
                type(exc).__name__,
            )
            recorded = False
        if not recorded:
            return GatewayOutcome(
                status="sending",
                child_id=child.child_id,
                broker_order_id=broker_order_id,
                result=result,
                outcome_not_recorded=True,
            )
        return GatewayOutcome(
            status=state.value,
            child_id=child.child_id,
            broker_order_id=broker_order_id,
            result=result,
        )

    async def _forward(
        self, request: Child, intent: IntentSnapshot, decision: Decision
    ) -> ForwardResult:
        forwarder = self._forwarders[intent.account]
        if isinstance(request, SubmitChild):
            return await forwarder.submit(intent, request)
        order_id = decision.target_broker_order_id
        if order_id is None:
            raise RuntimeError("allowed amend or cancel has no target order id")
        if isinstance(request, AmendChild):
            return await forwarder.amend(intent, request, broker_order_id=order_id)
        return await forwarder.cancel(intent, broker_order_id=order_id)
