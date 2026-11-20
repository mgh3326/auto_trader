"""hk 5441 — forward an ``upbit_live`` child through the existing Upbit order path.

Operator decision ``decision/2026-11-20/at-5438-d1-d19`` (D3): this is a real
account. It calls the existing order functions and changes none of their guards.
What it adds is in front of them:

* the child key is the Upbit ``identifier`` (I5): Upbit refuses a reused
  identifier, which is the second defence against a double send;
* the price and volume the gateway approved are the ones posted, unadjusted,
  or nothing is posted (B2, B28);
* a last look immediately before the request (design §4.4): the live switch, the
  account kill row, the intent's expiry, the mandate and its revocation, the
  sleeve state, an open exit handoff, and the account fingerprint (B12);
* for a cancel, the account fingerprint alone, at the same place: the
  transport's pre-send hook, immediately before the DELETE.

**What counts as proof that nothing was sent (B29).** The order layer calls the
pre-send hook immediately before the HTTP request and nowhere else. If the hook
never returned normally, the request was not made: either a guard below refused
first (``lower_guard_refused``) or the hook itself refused (its reason). That is
the same proof ``_place_order_impl`` already uses for its own
``proven_not_sent`` record. Once the hook has returned, anything short of an
accepted order is an unknown outcome, exactly as for kis_mock.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Protocol
from uuid import UUID

from app.schemas.execution_intent import CancelChild, UpbitSubmitChild
from app.services.execution_intent_gateway.decisions import IntentSnapshot, Refusal
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.upbit_decisions import (
    NOT_SENT_REASONS,
    AccountReads,
    UpbitFacts,
    check_before_send,
    not_sent_reason,
)
from app.services.kis_mock_runner.control import (
    KillSwitchStore,
    read_effective_kill_switch,
)

logger = logging.getLogger(__name__)

UPBIT_LIVE_ENABLED_ENV: Final = "EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED"
_MARKET: Final = "crypto"


def live_enabled(environment: Mapping[str, str]) -> bool:
    """The live switch (§8.2-4): exactly ``true``, default off."""
    return environment.get(UPBIT_LIVE_ENABLED_ENV) == "true"


class SendFacts(Protocol):
    async def facts_before_send(
        self, intent_id: UUID, *, now: datetime, exclude_child_id: int | None = None
    ) -> tuple[IntentSnapshot, UpbitFacts] | None: ...

    async def mandate_account_ref(self, intent_id: UUID) -> str | None: ...


def buy_thesis(intent: IntentSnapshot) -> str:
    """The journal thesis of an intent's order.

    The order service refuses a real buy without one. A mechanical strategy's
    reason for a trade is its decision row, which the intent already names
    (design §6.4), so the thesis is that reference and nothing invented here.
    """
    return (
        f"intent {intent.intent_id}: strategy {intent.strategy} "
        f"decision {intent.decision_ref}"
    )


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class _PreSendGate:
    """The pre-send hook. ``passed`` is True only after it returned normally."""

    def __init__(
        self,
        *,
        intent_id: UUID,
        child_id: int,
        quantity: Decimal,
        facts: SendFacts,
        kill_switch: KillSwitchStore,
        environment: Mapping[str, str],
        clock: Callable[[], datetime],
        account_ref: Callable[[], str | None],
    ) -> None:
        self._intent_id = intent_id
        self._child_id = child_id
        self._quantity = quantity
        self._facts = facts
        self._kill_switch = kill_switch
        self._environment = environment
        self._clock = clock
        self._account_ref = account_ref
        self.passed = False
        self.refusal: Refusal | None = None

    async def _refusal(self) -> Refusal | None:
        if not live_enabled(self._environment):
            return Refusal.UPBIT_LIVE_DISABLED
        kill_mode = (await read_effective_kill_switch(self._kill_switch)).mode
        loaded = await self._facts.facts_before_send(
            self._intent_id, now=self._clock(), exclude_child_id=self._child_id
        )
        if loaded is None:
            return Refusal.MANDATE_MISSING
        intent, facts = loaded
        # The reads above waited on the database. Expiry is judged against the
        # clock as it is now, immediately before the request, not as it was
        # when they started.
        facts = replace(facts, now=self._clock())
        return check_before_send(
            intent,
            quantity=self._quantity,
            facts=facts,
            reads=AccountReads(kill_mode=kill_mode, live_enabled=True),
            configured_account_ref=self._account_ref(),
        )

    async def __call__(self) -> None:
        from app.services.brokers.kis.pre_send import PreSendFreshnessError

        try:
            refusal = await self._refusal()
        except Exception as exc:  # noqa: BLE001 - an unreadable check sends nothing
            logger.error(
                "intent gateway upbit pre-send check raised: error_class=%s",
                type(exc).__name__,
            )
            refusal = Refusal.SLEEVE_UNREADABLE
        if refusal is not None:
            self.refusal = refusal
            raise PreSendFreshnessError((refusal.value,))
        self.passed = True


class _CancelAccountGate:
    """The pre-send hook of a live cancel (B12).

    ``passed`` is True only after the account check returned no refusal,
    immediately before the DELETE. Until then no request was made.
    """

    def __init__(self, refusal: Callable[[], Awaitable[str | None]]) -> None:
        self._refusal = refusal
        self.passed = False
        self.refusal: str | None = None

    async def __call__(self) -> None:
        from app.services.brokers.kis.pre_send import PreSendFreshnessError

        refusal = await self._refusal()
        if refusal is not None:
            self.refusal = refusal
            raise PreSendFreshnessError((refusal,))
        self.passed = True

    def not_sent_reason(self) -> str:
        """Why nothing was sent: this hook's refusal, or a guard below it."""
        return self.refusal or Refusal.LOWER_GUARD_REFUSED.value


def _not_sent_reason(gate: _PreSendGate, result: Mapping[str, Any] | None) -> str:
    if gate.refusal is not None:
        return not_sent_reason(gate.refusal.value)
    if result is not None and result.get("pre_send_blocked") is True:
        for code in result.get("reason_codes") or ():
            if code in NOT_SENT_REASONS:
                return str(code)
    return Refusal.LOWER_GUARD_REFUSED.value


class UpbitLiveForwarder:
    def __init__(
        self,
        *,
        facts: SendFacts,
        kill_switch: KillSwitchStore,
        environment: Mapping[str, str],
        clock: Callable[[], datetime],
        account_ref: Callable[[], str | None],
    ) -> None:
        self._facts = facts
        self._kill_switch = kill_switch
        self._environment = environment
        self._clock = clock
        self._account_ref = account_ref

    async def submit(
        self, intent: IntentSnapshot, request: UpbitSubmitChild, *, child_id: int
    ) -> ForwardResult:
        from app.mcp_server.tooling.crypto_prevalidated_wire import (
            PrevalidatedCryptoWire,
        )
        from app.mcp_server.tooling.order_execution import _place_order_impl

        quantity = Decimal(request.quantity_decimal)
        gate = _PreSendGate(
            intent_id=intent.intent_id,
            child_id=child_id,
            quantity=quantity,
            facts=self._facts,
            kill_switch=self._kill_switch,
            environment=self._environment,
            clock=self._clock,
            account_ref=self._account_ref,
        )
        if intent.execution_mode != "live" or not live_enabled(self._environment):
            # §8.2-4, checked again here: the order service is not called at all.
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False},
                not_sent_reason=Refusal.UPBIT_LIVE_DISABLED.value,
            )
        try:
            result = await _place_order_impl(
                symbol=intent.symbol,
                side=intent.side,
                market=_MARKET,
                order_type="limit",
                quantity=float(quantity),
                price=float(request.limit_price_krw),
                dry_run=False,
                reason=f"intent_gateway:{intent.intent_id}",
                thesis=buy_thesis(intent),
                strategy=intent.strategy,
                client_order_id=request.idempotency_key,
                pre_send_hook=gate,
                prevalidated_wire=PrevalidatedCryptoWire(
                    price_krw=request.limit_price_krw, volume=quantity
                ),
            )
        except Exception as exc:
            if gate.passed:
                raise
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False, "error_class": type(exc).__name__},
                not_sent_reason=_not_sent_reason(gate, None),
            )
        if not gate.passed:
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result=result,
                not_sent_reason=_not_sent_reason(gate, result),
            )
        # The ledger writer reports success even when the broker rejected.
        accepted = (
            result.get("success") is True and result.get("broker_status") == "accepted"
        )
        return ForwardResult(
            accepted=accepted,
            broker_order_id=_text_or_none(result.get("order_id")),
            result=result,
        )

    async def cancel(
        self, intent: IntentSnapshot, request: CancelChild, *, broker_order_id: str
    ) -> ForwardResult:
        from app.mcp_server.tooling.orders_modify_cancel import cancel_order_impl

        if intent.execution_mode != "live" or not live_enabled(self._environment):
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False},
                not_sent_reason=Refusal.UPBIT_LIVE_DISABLED.value,
            )
        # B12 for a cancel: the DELETE goes to the account the mandate was
        # approved for, or nowhere. Kill, expiry and revocation are not read --
        # a cancel passes all three -- only whose account this is. It is asked
        # twice: here, before the order layer is entered at all, and again in
        # the transport's pre-send hook, after the rate limiter has admitted
        # the request and immediately before the DELETE. The second look is the
        # one that authorizes the send: the first can be seconds old by then.
        mismatch = await self._cancel_account_refusal(intent)
        if mismatch is not None:
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False},
                not_sent_reason=mismatch,
            )
        gate = _CancelAccountGate(lambda: self._cancel_account_refusal(intent))
        try:
            result = await cancel_order_impl(
                order_id=broker_order_id,
                symbol=intent.symbol,
                market=_MARKET,
                pre_send_hook=gate,
            )
        except Exception as exc:
            if gate.passed:
                raise
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result={"success": False, "error_class": type(exc).__name__},
                not_sent_reason=gate.not_sent_reason(),
            )
        if not gate.passed:
            # The order layer turns the hook's refusal into an ordinary failed
            # result, so the proof is the hook's own flag: it is called
            # immediately before the request and nowhere else (B29).
            return ForwardResult(
                accepted=False,
                broker_order_id=None,
                result=result,
                not_sent_reason=gate.not_sent_reason(),
            )
        # The hook returned: the DELETE may have crossed the wire. Anything
        # short of success is an unknown outcome, as before.
        return ForwardResult(
            accepted=result.get("success") is True,
            broker_order_id=broker_order_id,
            result=result,
        )

    async def _cancel_account_refusal(self, intent: IntentSnapshot) -> str | None:
        try:
            approved = await self._facts.mandate_account_ref(intent.intent_id)
            configured = self._account_ref()
        except Exception as exc:  # noqa: BLE001 - an unreadable check sends nothing
            logger.error(
                "intent gateway upbit cancel account check raised: error_class=%s",
                type(exc).__name__,
            )
            return Refusal.ACCOUNT_MISMATCH.value
        if not approved or not configured or approved != configured:
            return Refusal.ACCOUNT_MISMATCH.value
        return None
