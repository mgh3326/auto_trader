"""hk 5441 — a go sleeve's intent orders: resolution, the shadow book and the
sleeve views the gateway reads (design §1.4, §3.6, B3, B5, B18, S2, I4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.execution_intent_gateway.upbit_store import sleeve_symbol_views
from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.intent_orders import (
    ChildOrderEvidence,
    IntentChildOrder,
    build_child_orders,
    child_order_key,
    shadow_book_inputs,
    unresolved_orders,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)
INTENT = uuid4()


def order(
    child_id: int = 1,
    *,
    state: str = "accepted",
    side: str = "buy",
    symbol: str = "KRW-XRP",
    quantity: str = "10",
    price: str = "3000",
    uuid: str | None = "uuid-1",
    evidence: tuple[str, str, str] | None = None,
    intent_id: UUID = INTENT,
    at: datetime = NOW,
) -> IntentChildOrder:
    return IntentChildOrder(
        child_id=child_id,
        intent_id=intent_id,
        idempotency_key=f"child-{child_id:04d}",
        symbol=symbol,
        side=side,
        execution_mode="shadow" if state == "shadowed" else "live",
        decision_ref="decision-1",
        state=state,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        broker_order_id=uuid,
        intent_created_at=NOW,
        created_at=at,
        finished_at=at,
        evidence=(
            None
            if evidence is None
            else ChildOrderEvidence(
                order_uuid=evidence[0],
                broker_state=evidence[1],
                executed_volume=Decimal(evidence[2]),
                remaining_volume=None,
                observed_at=NOW,
            )
        ),
    )


def test_a_child_order_key_never_collides_with_a_rung_id() -> None:
    assert child_order_key(7) == -7 and child_order_key(7) < 0


@pytest.mark.parametrize("state", ["accepted", "uncertain", "sending"])
def test_an_order_is_unresolved_until_the_broker_proves_its_end(state: str) -> None:
    assert order(state=state).resolved is False
    assert order(state=state).filled_quantity is None
    assert order(state=state, evidence=("uuid-1", "wait", "3")).resolved is False


@pytest.mark.parametrize("broker_state", ["done", "cancel"])
def test_a_final_broker_state_resolves_with_the_traded_quantity(broker_state) -> None:
    ended = order(evidence=("uuid-1", broker_state, "4"))
    assert ended.resolved is True and ended.filled_quantity == Decimal("4")
    book_order = ended.as_sleeve_order()
    assert book_order.state == "cancelled" and book_order.rung_filled_qty == Decimal(
        "4"
    )
    assert order(evidence=("uuid-1", "done", "10")).as_sleeve_order().state == "filled"


def test_b3_an_unrecorded_send_is_bound_by_the_identifier_evidence() -> None:
    found = order(state="sending", uuid=None, evidence=("uuid-found", "cancel", "2"))
    assert found.order_uuid == "uuid-found" and found.resolved is True


def test_b3_an_unbound_send_makes_the_book_unmeasured() -> None:
    unbound = order(state="sending", uuid=None)
    book_order = unbound.as_sleeve_order()
    assert unbound.order_uuid is None and book_order.fill_unproven is True
    book = compute_sleeve_book(orders=[book_order], fills=[], marks={})
    assert book.used is None and book.net_pnl is None
    assert book.unproven_orders == (child_order_key(1),)


def test_two_different_uuids_bind_nothing() -> None:
    torn = order(evidence=("uuid-other", "done", "10"))
    assert torn.order_uuid is None and torn.resolved is False
    assert torn.as_sleeve_order().fill_unproven is True


def test_a_resting_order_with_a_uuid_is_measured_as_open() -> None:
    resting = order().as_sleeve_order()
    assert resting.state == "resting" and resting.fill_unproven is False
    book = compute_sleeve_book(orders=[resting], fills=[], marks={})
    assert book.used == Decimal("30000") and book.open_order_notional == Decimal(
        "30000"
    )


def test_b5_unresolved_orders_are_per_symbol_and_ignore_shadow() -> None:
    orders = [
        order(1),
        order(2, symbol="KRW-BTC", evidence=("uuid-1", "done", "10")),
        order(3, state="shadowed", uuid=None),
    ]
    assert [o.child_id for o in unresolved_orders(orders, symbol="KRW-XRP")] == [1]
    assert unresolved_orders(orders, symbol="KRW-BTC") == []


def test_b18_the_shadow_position_is_the_fifo_remainder() -> None:
    orders = [
        order(1, state="shadowed", uuid=None, quantity="10", price="3000"),
        order(
            2,
            state="shadowed",
            uuid=None,
            quantity="10",
            price="3300",
            at=NOW + timedelta(hours=1),
        ),
        order(
            3,
            state="shadowed",
            uuid=None,
            side="sell",
            quantity="10",
            price="3600",
            at=NOW + timedelta(hours=2),
        ),
        order(4),  # a live order is not part of the shadow book
    ]
    book_orders, fills = shadow_book_inputs(orders)
    book = compute_sleeve_book(orders=book_orders, fills=fills, marks={})
    [position] = book.positions
    # The first lot (3000) was sold; the 3300 lot is what is still held.
    assert (position.quantity, position.cost_basis) == (Decimal("10"), Decimal("33000"))
    assert book.realized_pnl == Decimal("6000")
    assert len(book_orders) == 3


def test_build_child_orders_keeps_only_orders() -> None:
    def child(child_id: int, action: str, state: str, quantity: str | None):
        return SimpleNamespace(
            id=child_id,
            action=action,
            state=state,
            idempotency_key=f"k{child_id}",
            quantity_decimal=None if quantity is None else Decimal(quantity),
            limit_price_krw=3000 if quantity else None,
            broker_order_id=None,
            created_at=NOW,
            finished_at=NOW,
        )

    intent = SimpleNamespace(
        id=INTENT,
        symbol="KRW-XRP",
        side="buy",
        execution_mode="live",
        decision_ref="d",
        created_at=NOW,
    )
    rows = [
        (intent, child(1, "submit", "accepted", "10")),
        (intent, child(2, "submit", "refused", "10")),
        (intent, child(3, "submit", "not_sent", "10")),
        (intent, child(4, "cancel", "accepted", None)),
        (intent, child(5, "submit", "shadowed", "10")),
    ]
    assert [o.child_id for o in build_child_orders(rows, [])] == [1, 5]


# ------------------------------------------------------------ gateway views


def _intent_row(side: str, *, symbol: str = "KRW-XRP", expires_in: int = 3600, **extra):
    values = {
        "id": uuid4(),
        "side": side,
        "symbol": symbol,
        "origin": "strategy",
        "expires_at": NOW + timedelta(seconds=expires_in),
        "max_notional_krw": 480_000,
    }
    return SimpleNamespace(**{**values, **extra})


def _judged(side: str = "buy"):
    return SimpleNamespace(
        intent_id=uuid4(), symbol="KRW-XRP", side=side, execution_mode="live"
    )


def _views(intents, orders, *, book=None, judged=None):
    return sleeve_symbol_views(
        intent=judged or _judged(),
        intents=intents,
        orders=orders,
        book=book
        if book is not None
        else compute_sleeve_book(orders=[], fills=[], marks={}),
        handoff_active=False,
        now=NOW,
    )


def test_s2_a_stored_unexpired_sell_intent_is_open() -> None:
    stored = _intent_row("sell")
    expired = _intent_row("sell", expires_in=-1)
    other_symbol = _intent_row("sell", symbol="KRW-BTC")
    symbol, _buys, _reserved = _views([stored, expired, other_symbol], [])
    assert symbol.open_sell_intents == frozenset({stored.id})


def test_s2_an_expired_sell_intent_with_an_unresolved_order_is_still_open() -> None:
    expired = _intent_row("sell", expires_in=-1)
    symbol, _b, _r = _views([expired], [order(side="sell", intent_id=expired.id)])
    assert symbol.open_sell_intents == frozenset({expired.id})
    assert symbol.unresolved_prior_order is True and symbol.holdings is None


def test_s2_a_sell_intent_whose_orders_all_ended_is_done() -> None:
    done = _intent_row("sell")
    ended = order(side="sell", intent_id=done.id, evidence=("uuid-1", "done", "10"))
    symbol, _b, _r = _views([done], [ended])
    assert symbol.open_sell_intents == frozenset()
    assert symbol.unresolved_prior_order is False


def test_i4_other_open_buy_intents_reserve_what_they_have_not_sent() -> None:
    untouched = _intent_row("buy")
    partly_sent = _intent_row("buy", symbol="KRW-BTC")
    expired = _intent_row("buy", expires_in=-1)
    sent = order(
        2, intent_id=partly_sent.id, symbol="KRW-BTC", quantity="10", price="3000"
    )
    _symbol, open_buys, reserved = _views([untouched, partly_sent, expired], [sent])
    assert open_buys == 2
    assert reserved == Decimal("480000") + Decimal("450000")


def test_the_judged_intent_does_not_reserve_against_itself() -> None:
    judged = _judged()
    own = _intent_row("buy", id=judged.intent_id)
    _symbol, open_buys, reserved = _views([own], [], judged=judged)
    assert (open_buys, reserved) == (0, Decimal("0"))


def test_holdings_are_unknown_when_the_book_is_unreadable_or_unproven() -> None:
    unproven = compute_sleeve_book(
        orders=[order(state="sending", uuid=None).as_sleeve_order()], fills=[], marks={}
    )
    for book in (None, unproven):
        symbol, _b, _r = sleeve_symbol_views(
            intent=_judged("sell"),
            intents=[],
            orders=[],
            book=book,
            handoff_active=False,
            now=NOW,
        )
        assert symbol.holdings is None


# ------------------------------------------------------------ card sleeves unchanged


class _EmptyResult:
    def all(self) -> list:
        return []

    def scalars(self) -> _EmptyResult:
        return self


class _CountingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(str(statement))
        return _EmptyResult()


@pytest.mark.asyncio
async def test_a_card_sleeve_reads_nothing_of_the_second_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``card`` sleeve's attribution is the one query it always was: no intent
    child and no handoff is read, so its result cannot change (design §1.4).
    """
    from app.services.strategy_sleeves import store

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("a card sleeve must not read intent orders")

    monkeypatch.setattr(store, "load_intent_child_orders", must_not_run)
    monkeypatch.setattr(store, "_mark_unmeasured_handoff_cards", must_not_run)
    session = _CountingSession()
    sleeve = SimpleNamespace(
        id=1,
        account_mode="upbit",
        market="crypto",
        currency="KRW",
        execution_path="card",
    )

    inputs = await store.load_sleeve_inputs(session, sleeve)  # type: ignore[arg-type]

    assert inputs.orders == () and inputs.fills == ()
    assert len(session.statements) == 1
    assert "order_proposal_rungs" in session.statements[0]
    assert "execution_child_requests" not in session.statements[0]


@pytest.mark.asyncio
async def test_a_go_sleeve_reads_its_intent_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.strategy_sleeves import store

    async def orders(_session, _sleeve_id):
        return [
            order(evidence=("uuid-1", "done", "10")),
            order(2, state="shadowed", uuid=None),
        ]

    async def unchanged(_session, _sleeve, existing):
        return existing

    monkeypatch.setattr(store, "load_intent_child_orders", orders)
    monkeypatch.setattr(store, "_mark_unmeasured_handoff_cards", unchanged)
    session = _CountingSession()
    sleeve = SimpleNamespace(
        id=1,
        account_mode="upbit",
        market="crypto",
        currency="KRW",
        execution_path="go",
    )

    inputs = await store.load_sleeve_inputs(session, sleeve)  # type: ignore[arg-type]

    # The live order is in the book; the shadow one never is.
    assert [o.rung_id for o in inputs.orders] == [child_order_key(1)]
    assert inputs.orders[0].state == "filled"
