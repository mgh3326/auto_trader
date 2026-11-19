"""hk 5441 — accept one child of an ``upbit_live`` intent and forward it once.

The order of work is E1's (``gateway.py``), with the account's own gates:

1. The gateway's env gate and per-call confirm already passed (``IntentGateway``).
2. Reads outside the database: the account kill row (submit only), the live
   switch, and for a sell the account's average buy price, for a buy the
   stop-loss cooldown. None of them is trusted later: the order path below
   checks its own guards again.
3. Reserve under the sleeve, mandate and intent locks: replay a known key, or
   decide and commit the journal row. Nothing reaches a broker before that
   commit.
4. Forward once. The forwarder is chosen by the intent's ``execution_mode`` and
   by nothing else (I6): a shadow intent can only reach the shadow forwarder.
5. Record the outcome: ``shadowed``, ``accepted``, ``uncertain``, or
   ``not_sent`` when the request provably never left (B29).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from app.schemas.execution_intent import (
    AnyChild,
    CancelChild,
    UpbitSubmitChild,
    contract_digest,
)
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.gateway import (
    GatewayOutcome,
    _bounded_result,
    _refused,
    _replay,
)
from app.services.execution_intent_gateway.upbit_decisions import (
    AccountReads,
    UpbitDecision,
    UpbitFacts,
    decide_upbit_child,
)
from app.services.execution_intent_gateway.upbit_live_forwarder import live_enabled
from app.services.execution_intent_gateway.upbit_store import UpbitReservation
from app.services.kis_mock_runner.control import (
    KillMode,
    KillSwitchStore,
    read_effective_kill_switch,
)

logger = logging.getLogger(__name__)

#: The account's average buy price for a KRW market symbol, or ``None`` when it
#: is missing or could not be read (B9).
AvgPriceReader = Callable[[str], Awaitable[Decimal | None]]
#: Whether a symbol is in its stop-loss cooldown.
CooldownReader = Callable[[str], Awaitable[bool]]


class UpbitJournal(Protocol):
    async def account_of(self, intent_id: UUID) -> str | None: ...

    async def key_is_journaled(self, idempotency_key: str) -> bool: ...

    async def read_intent(self, intent_id: UUID) -> IntentSnapshot | None: ...

    async def reserve(
        self,
        request: AnyChild,
        *,
        request_digest: str,
        decide: Callable[[IntentSnapshot, Any, UpbitFacts], UpbitDecision],
        now_fn: Callable[[], datetime],
    ) -> UpbitReservation: ...

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
        refusal_reason: str | None = None,
    ) -> bool: ...


class ShadowForwarder(Protocol):
    async def submit(self, intent: Any, request: Any) -> ForwardResult: ...


class LiveForwarder(Protocol):
    async def submit(
        self, intent: IntentSnapshot, request: UpbitSubmitChild, *, child_id: int
    ) -> ForwardResult: ...

    async def cancel(
        self, intent: IntentSnapshot, request: CancelChild, *, broker_order_id: str
    ) -> ForwardResult: ...


class UpbitGatewayPath:
    def __init__(
        self,
        *,
        store: UpbitJournal,
        kill_switch: KillSwitchStore,
        shadow_forwarder: ShadowForwarder,
        live_forwarder: LiveForwarder | None,
        avg_price_reader: AvgPriceReader,
        cooldown_reader: CooldownReader,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._kill_switch = kill_switch
        self._shadow = shadow_forwarder
        self._live = live_forwarder
        self._avg_price_reader = avg_price_reader
        self._cooldown_reader = cooldown_reader
        self._environment = os.environ if environment is None else environment
        self._clock = clock

    async def account_of(self, intent_id: UUID) -> str | None:
        return await self._store.account_of(intent_id)

    async def key_is_journaled(self, idempotency_key: str) -> bool:
        return await self._store.key_is_journaled(idempotency_key)

    async def _account_reads(
        self, request: AnyChild, intent: IntentSnapshot | None
    ) -> AccountReads:
        kill_mode: KillMode | None = None
        if request.action != "cancel":
            kill_mode = (await read_effective_kill_switch(self._kill_switch)).mode
        avg_price: Decimal | None = None
        cooldown = False
        if intent is not None and isinstance(request, UpbitSubmitChild):
            if intent.side == "sell":
                avg_price = await self._read_avg_price(intent.symbol)
            elif intent.side == "buy":
                cooldown = await self._read_cooldown(intent.symbol)
        return AccountReads(
            kill_mode=kill_mode,
            live_enabled=live_enabled(self._environment),
            avg_buy_price=avg_price,
            cooldown_active=cooldown,
        )

    async def _read_avg_price(self, symbol: str) -> Decimal | None:
        try:
            value = await self._avg_price_reader(symbol)
        except Exception as exc:  # noqa: BLE001 - an unread price is unproven (B9)
            logger.warning(
                "intent gateway avg price read failed: error_class=%s",
                type(exc).__name__,
            )
            return None
        if value is None or not value.is_finite() or value <= 0:
            return None
        return value

    async def _read_cooldown(self, symbol: str) -> bool:
        try:
            return bool(await self._cooldown_reader(symbol))
        except Exception as exc:  # noqa: BLE001 - an unread cooldown blocks the buy
            logger.warning(
                "intent gateway cooldown read failed: error_class=%s",
                type(exc).__name__,
            )
            return True

    async def handle(self, request: AnyChild) -> GatewayOutcome:
        intent = await self._store.read_intent(request.intent_id)
        if (
            intent is not None
            and intent.execution_mode == "live"
            and self._live is None
        ):
            return _refused(Refusal.FORWARDER_UNAVAILABLE)
        reads = await self._account_reads(request, intent)
        reservation = await self._store.reserve(
            request,
            request_digest=contract_digest(request),
            now_fn=self._clock,
            decide=lambda stored, children, facts: decide_upbit_child(
                request, intent=stored, children=children, facts=facts, reads=reads
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
            if (
                child.state is ChildState.SENDING
                and intent is not None
                and intent.execution_mode == "shadow"
                and isinstance(request, UpbitSubmitChild)
            ):
                # A shadow child never reaches a broker, so a row left
                # ``sending`` by a crash or a failed write has nothing unknown
                # about it: the same key finishes it now. A live row in that
                # state is never touched here (I5).
                outcome = await self._forward_and_record(
                    request, intent, child, UpbitDecision()
                )
                if not outcome.outcome_not_recorded:
                    return outcome
            return _replay(child)
        decision = reservation.decision
        if decision is None or reservation.intent is None:
            raise RuntimeError("gateway store returned an incomplete reservation")
        if decision.refusal is not None:
            return _refused(decision.refusal, child.child_id)
        return await self._forward_and_record(
            request, reservation.intent, child, decision
        )

    async def _forward(
        self,
        request: AnyChild,
        intent: IntentSnapshot,
        child: ChildSnapshot,
        decision: UpbitDecision,
    ) -> ForwardResult:
        # I6: the mode alone picks the forwarder. A shadow intent never gets the
        # live one, whatever the request or the configuration says.
        if intent.execution_mode == "shadow":
            if not isinstance(request, UpbitSubmitChild):
                raise RuntimeError("a shadow intent has no broker order to act on")
            return await self._shadow.submit(intent, request)
        if intent.execution_mode != "live" or self._live is None:
            raise RuntimeError("no forwarder for this execution mode")
        if isinstance(request, UpbitSubmitChild):
            return await self._live.submit(intent, request, child_id=child.child_id)
        if not isinstance(request, CancelChild):
            raise RuntimeError("an upbit child is a submit or a cancel")
        order_id = decision.target_broker_order_id
        if order_id is None:
            raise RuntimeError("allowed cancel has no target order id")
        return await self._live.cancel(intent, request, broker_order_id=order_id)

    async def _forward_and_record(
        self,
        request: AnyChild,
        intent: IntentSnapshot,
        child: ChildSnapshot,
        decision: UpbitDecision,
    ) -> GatewayOutcome:
        try:
            forwarded = await self._forward(request, intent, child, decision)
        except Exception as exc:  # noqa: BLE001 - the send may have crossed the wire
            logger.error(
                "intent gateway upbit forward raised: child_id=%s error_class=%s",
                child.child_id,
                type(exc).__name__,
            )
            forwarded = ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"error_class": type(exc).__name__},
            )

        reason: str | None = None
        broker_order_id = forwarded.broker_order_id
        if intent.execution_mode == "shadow":
            # Whatever a shadow forwarder returned, a shadow row records no
            # broker order; the journal trigger refuses anything else.
            state = ChildState.SHADOWED if forwarded.shadowed else ChildState.NOT_SENT
            broker_order_id = None
            if state is ChildState.NOT_SENT:
                reason = Refusal.LOWER_GUARD_REFUSED.value
        elif forwarded.not_sent_reason is not None:
            state = ChildState.NOT_SENT
            reason = forwarded.not_sent_reason
            broker_order_id = None
        else:
            needs_order_id = request.action != "cancel"
            accepted = forwarded.accepted and (
                broker_order_id is not None or not needs_order_id
            )
            state = ChildState.ACCEPTED if accepted else ChildState.UNCERTAIN
        result = _bounded_result(forwarded.result)

        try:
            recorded = await self._store.finish(
                child.child_id,
                state=state,
                broker_order_id=broker_order_id,
                result=result,
                now=self._clock(),
                refusal_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - the row stays sending, not resent
            logger.error(
                "intent gateway upbit outcome not recorded: child_id=%s error_class=%s",
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
        if state is ChildState.NOT_SENT:
            return GatewayOutcome(
                status="refused", reason=reason, child_id=child.child_id, result=result
            )
        return GatewayOutcome(
            status=state.value,
            child_id=child.child_id,
            broker_order_id=broker_order_id,
            result=result,
        )
