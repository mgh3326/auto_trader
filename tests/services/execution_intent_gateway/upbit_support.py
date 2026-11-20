"""Shared builders and fakes for the hk 5441 ``upbit_live`` tests.

``MemoryUpbitStore`` keeps the reserve contract of ``UpbitGatewayStore``: the
journal row exists before the decision returns, a known key replays, and the
facts a decision reads are whatever the test set on the store. The real store,
locks and triggers run in ``test_upbit_db.py`` against the run-owned database.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.schemas.execution_intent import AnyChild, CancelChild, UpbitSubmitChild
from app.services.execution_intent_gateway.decisions import (
    ChildSnapshot,
    ChildState,
    IntentSnapshot,
    Refusal,
)
from app.services.execution_intent_gateway.forward_result import ForwardResult
from app.services.execution_intent_gateway.upbit_decisions import (
    AccountReads,
    MandateView,
    OperatorExitView,
    SleeveView,
    SymbolView,
    UpbitDecision,
    UpbitFacts,
)
from app.services.execution_intent_gateway.upbit_store import UpbitReservation
from app.services.kis_mock_runner.control import KillMode
from app.services.strategy_sleeves.book import (
    SleeveBook,
    SleeveFill,
    SleeveOrder,
    compute_sleeve_book,
)
from app.services.strategy_sleeves.gate import SleeveVersionView

NOW = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)
CODE_SHA = "a" * 40
SYMBOL = "KRW-XRP"
ACCOUNT_REF = "upbit:upbit:" + "b" * 64
LIVE_ENV = {"EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED": "true"}


def upbit_intent(**overrides: Any) -> IntentSnapshot:
    values: dict[str, Any] = {
        "intent_id": uuid4(),
        "strategy": "upbit-trend-long-v1",
        "account": "upbit_live",
        "venue": "upbit",
        "symbol": SYMBOL,
        "side": "buy",
        "max_quantity": 0,
        "max_notional_krw": 480_000,
        "limit_price_krw": 3_000,
        "max_slippage_bps": 30,
        "position_at_authorization": 0,
        "min_position_after": 0,
        "max_position_after": 0,
        "expires_at": NOW + timedelta(hours=6),
        "schema_version": 2,
        "execution_mode": "live",
        "mandate_id": UUID(int=1),
        "sleeve_id": 7,
        "strategy_code_sha": CODE_SHA,
        "decision_ref": "upbit-trend:KRW-XRP:2026-11-20",
        "origin": "strategy",
        "operator_exit_id": None,
        "max_quantity_decimal": Decimal("160"),
        "position_at_authorization_decimal": Decimal("0"),
        "min_position_after_decimal": Decimal("0"),
        "max_position_after_decimal": Decimal("160"),
        "created_at": NOW,
    }
    return IntentSnapshot(**{**values, **overrides})


def sell_intent(**overrides: Any) -> IntentSnapshot:
    values: dict[str, Any] = {
        "side": "sell",
        "max_quantity_decimal": Decimal("100"),
        "position_at_authorization_decimal": Decimal("100"),
        "min_position_after_decimal": Decimal("0"),
        "max_position_after_decimal": Decimal("100"),
    }
    return upbit_intent(**{**values, **overrides})


def mandate(**overrides: Any) -> MandateView:
    values: dict[str, Any] = {
        "mandate_id": UUID(int=1),
        "strategy": "upbit-trend-long-v1",
        "strategy_code_sha": CODE_SHA,
        "sleeve_id": 7,
        "account": "upbit_live",
        "mode": "live",
        "allowed_symbols": frozenset({"KRW-BTC", "KRW-ETH", "KRW-SOL", SYMBOL}),
        "per_intent_max_notional_krw": Decimal("480000"),
        "max_open_intents": 4,
        "price_band_bps": 30,
        "max_intent_ttl": timedelta(hours=12),
        "expires_at": NOW + timedelta(days=30),
        "account_ref": ACCOUNT_REF,
        "revoked": False,
    }
    return MandateView(**{**values, **overrides})


def version(state: str = "live", **overrides: Any) -> SleeveVersionView:
    values: dict[str, Any] = {
        "sleeve_id": 7,
        "version": 2,
        "state": state,
        "budget_cap": Decimal("1950000"),
        "loss_limit": Decimal("1000000"),
    }
    return SleeveVersionView(**{**values, **overrides})


def book(
    *,
    orders: Sequence[SleeveOrder] = (),
    fills: Sequence[SleeveFill] = (),
    marks: dict[str, Decimal | None] | None = None,
) -> SleeveBook:
    return compute_sleeve_book(orders=orders, fills=fills, marks=marks or {})


def held_book(
    symbol: str = SYMBOL, quantity: str = "100", price: str = "3000"
) -> SleeveBook:
    """A book holding one bought lot, marked at its cost."""
    order = SleeveOrder(
        proposal_pk=0,
        rung_id=-1,
        symbol=symbol,
        side="buy",
        state="filled",
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        notional=None,
        rung_filled_qty=Decimal(quantity),
    )
    fill = SleeveFill(
        ledger_id=1,
        rung_id=-1,
        symbol=symbol,
        side="buy",
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        filled_at=NOW,
    )
    return book(orders=[order], fills=[fill], marks={symbol: Decimal(price)})


def symbol_view(**overrides: Any) -> SymbolView:
    values: dict[str, Any] = {
        "handoff_active": False,
        "unresolved_prior_order": False,
        "open_sell_intents": frozenset(),
        "holdings": Decimal("0"),
    }
    return SymbolView(**{**values, **overrides})


def facts(**overrides: Any) -> UpbitFacts:
    values: dict[str, Any] = {
        "mandate": mandate(),
        "operator_exit": None,
        "sleeve": SleeveView(version=version(), book=book()),
        "symbol": symbol_view(),
        "open_buy_intents": 0,
        "now": NOW,
    }
    return UpbitFacts(**{**values, **overrides})


def reads(**overrides: Any) -> AccountReads:
    values: dict[str, Any] = {
        "kill_mode": KillMode.ACTIVE,
        "live_enabled": True,
        "avg_buy_price": Decimal("2500"),
        "cooldown_active": False,
    }
    return AccountReads(**{**values, **overrides})


def operator_exit(intent: IntentSnapshot, **overrides: Any) -> OperatorExitView:
    values: dict[str, Any] = {
        "exit_id": intent.operator_exit_id,
        "mandate_id": intent.mandate_id,
        "symbol": intent.symbol,
        "max_quantity": Decimal("100"),
        "expires_at": NOW + timedelta(minutes=30),
    }
    return OperatorExitView(**{**values, **overrides})


def submit(
    intent: IntentSnapshot, key: str = "child-0001", **overrides: Any
) -> UpbitSubmitChild:
    values: dict[str, Any] = {
        "action": "submit",
        "intent_id": intent.intent_id,
        "idempotency_key": key,
        "quantity_decimal": "10",
        "limit_price_krw": 3_000,
    }
    return UpbitSubmitChild(**{**values, **overrides})


def cancel(intent: IntentSnapshot, target: str, key: str = "child-0009") -> CancelChild:
    return CancelChild(
        action="cancel",
        intent_id=intent.intent_id,
        idempotency_key=key,
        target_idempotency_key=target,
    )


def child_row(
    key: str,
    *,
    state: ChildState = ChildState.ACCEPTED,
    action: str = "submit",
    quantity: str | None = "10",
    price: int | None = 3_000,
    broker_order_id: str | None = "uuid-1",
    target: str | None = None,
    child_id: int = 1,
    refusal_reason: str | None = None,
) -> ChildSnapshot:
    return ChildSnapshot(
        child_id=child_id,
        idempotency_key=key,
        request_digest="digest",
        action=action,
        target_idempotency_key=target,
        quantity=None,
        limit_price_krw=price,
        state=state,
        refusal_reason=refusal_reason,
        broker_order_id=broker_order_id,
        result=None,
        created_at=NOW,
        quantity_decimal=None if quantity is None else Decimal(quantity),
    )


class MemoryUpbitStore:
    """The journal in memory. ``facts`` is what every decision will read."""

    def __init__(
        self, *intents: IntentSnapshot, facts: UpbitFacts | None = None
    ) -> None:
        self.intents: dict[UUID, IntentSnapshot] = {i.intent_id: i for i in intents}
        self.children: list[ChildSnapshot] = []
        self.owner: dict[str, UUID] = {}
        self.facts: UpbitFacts = facts or globals()["facts"]()
        self.handoffs_opened: list[tuple[UUID, str]] = []
        self.sleeves_stopped: list[int] = []
        self.finish_fails = False
        self.events: list[str] = []

    def child(self, key: str) -> ChildSnapshot:
        return next(row for row in self.children if row.idempotency_key == key)

    async def account_of(self, intent_id: UUID) -> str | None:
        intent = self.intents.get(intent_id)
        return None if intent is None else intent.account

    async def key_is_journaled(self, idempotency_key: str) -> bool:
        return any(row.idempotency_key == idempotency_key for row in self.children)

    async def read_intent(self, intent_id: UUID) -> IntentSnapshot | None:
        return self.intents.get(intent_id)

    async def facts_before_send(
        self, intent_id: UUID, *, now: datetime, exclude_child_id: int | None = None
    ) -> tuple[IntentSnapshot, UpbitFacts] | None:
        intent = self.intents.get(intent_id)
        return None if intent is None else (intent, self.facts)

    async def mandate_account_ref(self, intent_id: UUID) -> str | None:
        mandate = self.facts.mandate
        return None if mandate is None else mandate.account_ref

    async def reserve(
        self,
        request: AnyChild,
        *,
        request_digest: str,
        decide: Callable[[IntentSnapshot, Any, UpbitFacts], UpbitDecision],
        now_fn: Callable[[], datetime],
    ) -> UpbitReservation:
        key = request.idempotency_key
        existing = next((r for r in self.children if r.idempotency_key == key), None)
        intent = self.intents.get(request.intent_id)
        if intent is None:
            if existing is not None:
                return UpbitReservation(child=existing)
            return UpbitReservation(refusal_without_row=Refusal.INTENT_NOT_FOUND)
        if existing is not None:
            if existing.request_digest != request_digest:
                return UpbitReservation(conflict=True)
            return UpbitReservation(child=existing, is_new=False)
        own = [
            r
            for r in self.children
            if self.owner[r.idempotency_key] == intent.intent_id
        ]
        decision = decide(intent, own, self.facts)
        if not decision.journal:
            return UpbitReservation(
                intent=intent, decision=decision, refusal_without_row=decision.refusal
            )
        quantity = getattr(request, "quantity_decimal", None)
        row = ChildSnapshot(
            child_id=len(self.children) + 1,
            idempotency_key=key,
            request_digest=request_digest,
            action=request.action,
            target_idempotency_key=getattr(request, "target_idempotency_key", None),
            quantity=None,
            limit_price_krw=getattr(request, "limit_price_krw", None),
            state=ChildState.REFUSED if decision.refusal else ChildState.SENDING,
            refusal_reason=decision.refusal.value if decision.refusal else None,
            broker_order_id=None,
            result=None,
            created_at=now_fn(),
            quantity_decimal=None if quantity is None else Decimal(quantity),
        )
        self.children.append(row)
        self.owner[key] = intent.intent_id
        self.events.append(f"journal:{key}:{row.state.value}")
        if decision.open_handoff and decision.refusal is not None:
            self.handoffs_opened.append((intent.intent_id, decision.refusal.value))
        if decision.stop_sleeve is not None:
            self.sleeves_stopped.append(decision.stop_sleeve.sleeve_id)
        return UpbitReservation(
            intent=intent, child=row, is_new=True, decision=decision
        )

    async def finish(
        self,
        child_id: int,
        *,
        state: ChildState,
        broker_order_id: str | None,
        result: dict[str, Any],
        now: datetime,
        refusal_reason: str | None = None,
    ) -> bool:
        if self.finish_fails:
            raise RuntimeError("journal write failed")
        index = child_id - 1
        row = self.children[index]
        if row.state is not ChildState.SENDING:
            return False
        self.children[index] = ChildSnapshot(
            child_id=row.child_id,
            idempotency_key=row.idempotency_key,
            request_digest=row.request_digest,
            action=row.action,
            target_idempotency_key=row.target_idempotency_key,
            quantity=None,
            limit_price_krw=row.limit_price_krw,
            state=state,
            refusal_reason=refusal_reason,
            broker_order_id=broker_order_id,
            result=result,
            created_at=row.created_at,
            quantity_decimal=row.quantity_decimal,
            finished_at=now,
        )
        self.events.append(f"finish:{row.idempotency_key}:{state.value}")
        return True


class FakeShadowForwarder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def submit(self, intent: Any, request: Any) -> ForwardResult:
        self.calls.append(request.idempotency_key)
        return ForwardResult(
            accepted=False, broker_order_id=None, result={"shadow": True}, shadowed=True
        )


class FakeLiveForwarder:
    """Answers with ``result``; records every call."""

    def __init__(self, result: ForwardResult | Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result or ForwardResult(
            accepted=True, broker_order_id="uuid-1", result={"success": True}
        )

    def _answer(self) -> ForwardResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def submit(
        self, intent: Any, request: Any, *, child_id: int
    ) -> ForwardResult:
        self.calls.append(("submit", request.idempotency_key))
        return self._answer()

    async def cancel(
        self, intent: Any, request: Any, *, broker_order_id: str
    ) -> ForwardResult:
        self.calls.append(("cancel", request.idempotency_key))
        return self._answer()
